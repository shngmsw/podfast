"""Step 01: 音声前処理 - 入力モード判定 + トラック分離 + 16kHz変換

入力モード:
1. --tracks "a.wav,b.wav"         → マルチトラック（話者別ファイル）
2. --input x.mp4 --stereo-split   → ステレオL/R分離
3. --input x.mp4                  → MP4内の音声トラック数を自動判定
   - 複数audio stream → OBSマルチトラックとして自動分離
   - 1 audio stream, stereo → シングルとして扱う（--stereo-splitなければ）
   - 1 audio stream, mono → シングル
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def get_probe(input_path: str) -> dict:
    # Windows パスのバックスラッシュを ffprobe に渡すと JSON 出力でエスケープ問題が起きるため
    # フォワードスラッシュに統一する
    safe_path = str(input_path).replace("\\", "/")
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        safe_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"[ERROR] ffprobe失敗: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def get_audio_streams(probe: dict) -> list[dict]:
    """音声ストリームだけ抽出"""
    return [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]


def extract_audio_stream(input_path: str, stream_index: int, output_path: str) -> None:
    """MP4等から特定の音声ストリームを16kHz mono WAVとして抽出"""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-map", f"0:a:{stream_index}",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] 音声抽出失敗 (stream {stream_index}): {result.stderr}", file=sys.stderr)
        sys.exit(1)


def convert_to_16k_mono(input_path: str, output_path: str, channel: int | None = None) -> None:
    """16kHz mono WAVに変換。channel指定時はそのチャンネルだけ抽出"""
    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    if channel is not None:
        if channel == 0:
            cmd += ["-af", "pan=mono|c0=FL"]
        else:
            cmd += ["-af", "pan=mono|c0=FR"]
    cmd += ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] FFmpeg変換失敗: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def make_mixdown(track_paths: list[str], output_path: str) -> None:
    """全トラックをミックスダウン（STTフォールバック用）"""
    inputs = []
    for t in track_paths:
        inputs += ["-i", t]
    n = len(track_paths)
    filter_str = "".join(f"[{i}:a]" for i in range(n))
    filter_str += f"amix=inputs={n}:duration=longest[out]"
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_str,
        "-map", "[out]", "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le", str(output_path)
    ]
    subprocess.run(cmd, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description="音声前処理")
    parser.add_argument("--input", default=None, help="入力ファイル（WAV/MP4/MKV等）")
    parser.add_argument("--tracks", default=None, help="カンマ区切りのトラックファイルパス")
    parser.add_argument("--stereo-split", action="store_true", help="ステレオをL/Rに分離")
    parser.add_argument("--speakers", default=None, help="カンマ区切りの話者名")
    parser.add_argument("--output", required=True, help="出力ディレクトリ")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    tracks_dir = output_dir / "tracks"
    tracks_dir.mkdir(exist_ok=True)

    speaker_names = [s.strip() for s in args.speakers.split(",")] if args.speakers else []
    track_files = []

    # =========================================================
    # モード判定
    # =========================================================

    if args.tracks:
        # --- 明示的マルチトラック ---
        mode = "multitrack"
        raw_paths = [p.strip() for p in args.tracks.split(",")]
        for i, raw_path in enumerate(raw_paths):
            name = speaker_names[i] if i < len(speaker_names) else f"SPEAKER_{i:02d}"
            wav_path = tracks_dir / f"track_{i:02d}_{name}_16k.wav"
            convert_to_16k_mono(raw_path, str(wav_path))
            track_files.append({"path": str(wav_path), "speaker": name, "source": raw_path})
            print(f"[INFO] トラック{i}: {raw_path} -> {wav_path} ({name})")

    elif args.input:
        probe = get_probe(args.input)
        audio_streams = get_audio_streams(probe)
        num_audio = len(audio_streams)

        if num_audio == 0:
            print("[ERROR] 音声ストリームが見つからない", file=sys.stderr)
            sys.exit(1)

        if num_audio >= 2 and not args.stereo_split:
            # --- OBSマルチトラック自動検出 ---
            mode = "obs_multitrack"
            print(f"[INFO] 音声ストリーム {num_audio}本検出 -> OBSマルチトラックとして分離")

            for i in range(num_audio):
                stream = audio_streams[i]
                # ストリームのタグから名前を取得（OBSはtitle等を設定可能）
                stream_title = stream.get("tags", {}).get("title", "")
                stream_title = stream_title or stream.get("tags", {}).get("handler_name", "")

                name = (speaker_names[i] if i < len(speaker_names)
                        else stream_title if stream_title
                        else f"SPEAKER_{i:02d}")
                # 名前をファイル名安全にする
                safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)

                wav_path = tracks_dir / f"track_{i:02d}_{safe_name}_16k.wav"
                extract_audio_stream(args.input, i, str(wav_path))

                ch = int(stream.get("channels", 1))
                sr = int(stream.get("sample_rate", 0))
                codec = stream.get("codec_name", "?")
                print(f"[INFO] Stream {i}: {codec} {sr}Hz {ch}ch"
                      f"{' (' + stream_title + ')' if stream_title else ''}"
                      f" -> {wav_path} ({name})")

                track_files.append({
                    "path": str(wav_path),
                    "speaker": name,
                    "source": args.input,
                    "stream_index": i,
                    "stream_title": stream_title,
                })

        elif args.stereo_split:
            # --- ステレオ分離 ---
            mode = "stereo_split"
            first_stream = audio_streams[0]
            channels = int(first_stream.get("channels", 1))
            if channels < 2:
                print("[ERROR] --stereo-split指定だが入力がモノラル", file=sys.stderr)
                sys.exit(1)

            for ch in range(2):
                name = speaker_names[ch] if ch < len(speaker_names) else f"SPEAKER_{ch:02d}"
                wav_path = tracks_dir / f"track_{ch:02d}_{name}_16k.wav"
                convert_to_16k_mono(args.input, str(wav_path), channel=ch)
                track_files.append({
                    "path": str(wav_path), "speaker": name,
                    "source": args.input, "channel": ch,
                })
                print(f"[INFO] Ch{ch} ({'LR'[ch]}): -> {wav_path} ({name})")

        else:
            # --- シングルトラック ---
            mode = "single"
            wav_path = tracks_dir / "track_00_mixed_16k.wav"
            convert_to_16k_mono(args.input, str(wav_path))
            track_files.append({"path": str(wav_path), "speaker": "mixed", "source": args.input})
            print(f"[INFO] シングルトラック: {args.input} -> {wav_path}")

    else:
        print("[ERROR] --input または --tracks を指定してください", file=sys.stderr)
        sys.exit(1)

    # =========================================================
    # メタデータ
    # =========================================================
    source_file = args.input or track_files[0]["source"]
    probe = get_probe(source_file)
    audio_streams = get_audio_streams(probe)
    duration = float(probe["format"].get("duration", 0))

    metadata = {
        "mode": mode,
        "num_tracks": len(track_files),
        "tracks": track_files,
        "duration_sec": round(duration, 2),
        "sample_rate": int(audio_streams[0].get("sample_rate", 0)) if audio_streams else 0,
        "speakers": [t["speaker"] for t in track_files],
        "source_files": list(set(
            t["source"] for t in track_files
        )),
        "audio_streams_in_source": len(audio_streams),
    }

    # マルチトラック時: ミックスダウンも作成
    if mode != "single":
        mix_path = tracks_dir / "mixed_16k.wav"
        make_mixdown([t["path"] for t in track_files], str(mix_path))
        metadata["mixed_path"] = str(mix_path)

    meta_path = output_dir / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"[INFO] モード: {mode}, トラック数: {len(track_files)}, 長さ: {duration:.1f}秒")
    if mode == "obs_multitrack":
        print(f"[INFO] OBS録音から {len(track_files)} 音声トラックを分離しました")
        if not speaker_names:
            print("[INFO] --speakers で話者名を指定すると、トランスクリプトに反映されます")


if __name__ == "__main__":
    main()
