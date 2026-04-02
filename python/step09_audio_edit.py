"""Step 09: 音声編集 - トラック別カット + クロストーク処理 + ミックスダウン + LUFS正規化

マルチトラック時の処理フロー:
1. 各トラックにカット提案を適用
2. クロストーク区間にダッキング/ミュートを適用
3. 全トラックをミックスダウン
4. LUFS -16 に正規化
5. MP3/M4Aエンコード
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def apply_cuts_to_track(input_path: str, keep_segments: list[dict], output_path: str) -> None:
    """1トラックにカット提案を適用（バッチ処理対応）"""
    if not keep_segments:
        print(f"[WARN] keep_segmentsが空: {input_path}", file=sys.stderr)
        return

    n = len(keep_segments)
    BATCH_SIZE = 200  # ffmpegのfilter_complex制限回避

    if n <= BATCH_SIZE:
        _apply_cuts_single(input_path, keep_segments, output_path)
    else:
        # バッチに分割して処理 → 最後に結合
        batch_files = []
        out_dir = Path(output_path).parent
        for batch_idx in range(0, n, BATCH_SIZE):
            batch_segs = keep_segments[batch_idx:batch_idx + BATCH_SIZE]
            batch_path = str(out_dir / f"_batch_{batch_idx}.wav")
            _apply_cuts_single(input_path, batch_segs, batch_path)
            batch_files.append(batch_path)
            print(f"  batch {batch_idx//BATCH_SIZE+1}/{(n+BATCH_SIZE-1)//BATCH_SIZE}")

        # バッチ結合
        if len(batch_files) == 1:
            shutil.move(batch_files[0], output_path)
        else:
            # concat demuxerで結合
            list_path = str(out_dir / "_concat_list.txt")
            with open(list_path, "w", encoding="utf-8") as f:
                for bf in batch_files:
                    abs_bf = str(Path(bf).resolve()).replace("\\", "/")
                    f.write(f"file '{abs_bf}'\n")
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c", "copy",
                str(output_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                print(f"[ERROR] バッチ結合失敗: {result.stderr}", file=sys.stderr)
                sys.exit(1)
            # 一時ファイル削除
            for bf in batch_files:
                os.remove(bf)
            os.remove(list_path)


def _apply_cuts_single(input_path: str, keep_segments: list[dict], output_path: str) -> None:
    """1バッチ分のカット適用"""
    n = len(keep_segments)

    filter_parts = []
    for i, seg in enumerate(keep_segments):
        filter_parts.append(
            f"[0:a]atrim=start={seg['start']}:end={seg['end']},asetpts=PTS-STARTPTS[s{i}]"
        )

    if n == 1:
        filter_complex = filter_parts[0]
        output_label = "[s0]"
    else:
        concat_inputs = "".join(f"[s{i}]" for i in range(n))
        filter_parts.append(f"{concat_inputs}concat=n={n}:v=0:a=1[out]")
        filter_complex = ";\n".join(filter_parts)
        output_label = "[out]"

    # filter_complexをファイルに書き出し（コマンドライン長制限回避）
    filter_path = str(Path(output_path).parent / "_filter.txt")
    with open(filter_path, "w", encoding="utf-8") as f:
        f.write(filter_complex)

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-filter_complex_script", filter_path,
        "-map", output_label,
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    os.remove(filter_path)
    if result.returncode != 0:
        print(f"[ERROR] カット適用失敗: {result.stderr[:500]}", file=sys.stderr)
        sys.exit(1)


def apply_crosstalk_processing(track_path: str, speaker: str,
                                crosstalk: list[dict], output_path: str) -> None:
    """クロストーク処理（ダッキング/ミュート）をトラックに適用

    volume filterで該当区間の音量を下げる/ゼロにする。
    """
    # この話者がtarget_speakerになっている区間を抽出
    actions = [ct for ct in crosstalk
               if ct.get("target_speaker") == speaker and ct["action"] in ("ducking", "mute")]

    if not actions:
        # 処理不要: そのままコピー
        shutil.copy2(track_path, output_path)
        return

    # volume filterを構築
    # enable='between(t,start,end)' で区間指定
    filter_chain = ""
    for act in actions:
        start = act["start"]
        end = act["end"]
        if act["action"] == "mute":
            vol = 0
        else:
            db = act.get("ducking_db", -12)
            vol = 10 ** (db / 20)  # dBをリニアに変換

        if filter_chain:
            filter_chain += ","
        filter_chain += f"volume=enable='between(t,{start},{end})':volume={vol:.4f}"

    if not filter_chain:
        shutil.copy2(track_path, output_path)
        return

    cmd = [
        "ffmpeg", "-y", "-i", str(track_path),
        "-af", filter_chain,
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"[ERROR] クロストーク処理失敗 ({speaker}): {result.stderr}", file=sys.stderr)
        sys.exit(1)


def mixdown_tracks(track_paths: list[str], output_path: str) -> None:
    """複数トラックをミックスダウン（各トラック-3dBで合成）"""
    if len(track_paths) == 1:
        shutil.copy2(track_paths[0], output_path)
        return

    n = len(track_paths)
    inputs = []
    for t in track_paths:
        inputs += ["-i", t]

    # 各トラックを-3dB (0.7079)にして合成。片方のみ時-3dB、両方時±0dB。
    filter_parts = []
    for i in range(n):
        filter_parts.append(f"[{i}:a]volume=0.7079[s{i}]")
    input_labels = "".join(f"[s{i}]" for i in range(n))
    filter_parts.append(f"{input_labels}amix=inputs={n}:duration=longest:normalize=0[out]")
    filter_str = ";\n".join(filter_parts)

    # filter_complex_scriptファイル経由で実行
    filter_path = str(Path(output_path).parent / "_mix_filter.txt")
    with open(filter_path, "w", encoding="utf-8") as f:
        f.write(filter_str)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex_script", filter_path,
        "-map", "[out]",
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    os.remove(filter_path)
    if result.returncode != 0:
        print(f"[ERROR] ミックスダウン失敗: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def parse_loudnorm_stats(stderr_text: str) -> dict | None:
    """ffmpeg stderrからloudnorm測定値JSONを抽出"""
    import re
    match = re.search(r'\{[^}]*"input_i"[^}]*\}', stderr_text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return None


def normalize_and_encode(input_path: str, output_path: str,
                          target_lufs: float = -16, fmt: str = "mp3") -> None:
    """2パス ラウドネス正規化 + エンコード"""
    if fmt == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "320k"]
    else:
        codec_args = ["-c:a", "aac", "-b:a", "256k"]

    # パス1: 測定
    print("[INFO] ラウドネス測定中 (pass 1/2)...")
    cmd_measure = [
        "ffmpeg", "-i", str(input_path),
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", os.devnull
    ]
    result = subprocess.run(cmd_measure, capture_output=True, text=True, encoding="utf-8", errors="replace")
    measured = parse_loudnorm_stats(result.stderr)

    if measured:
        # パス2: 測定値を使って線形補正
        print(f"[INFO] 測定完了: I={measured['input_i']}LUFS, TP={measured['input_tp']}dB")
        af_filter = (
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
            f":measured_I={measured['input_i']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_thresh={measured['input_thresh']}"
            f":linear=true"
        )
        print("[INFO] ラウドネス補正 + エンコード中 (pass 2/2)...")
    else:
        # フォールバック: 1パス
        print("[WARN] 測定値取得失敗、1パスモードで実行")
        af_filter = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-af", af_filter,
        "-ar", "48000",
        *codec_args,
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"[ERROR] エンコード失敗: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="音声編集 + クロストーク処理 + ラウドネス正規化")
    parser.add_argument("--metadata", required=True, help="step01のmetadata.json")
    parser.add_argument("--cuts", required=True, help="cut_proposal.json")
    parser.add_argument("--crosstalk", default=None, help="crosstalk_result.json")
    parser.add_argument("--tracks-dir", default=None, help="高音質トラックディレクトリ（48kHz WAV等）")
    parser.add_argument("--output", required=True, help="出力ディレクトリ")
    parser.add_argument("--format", default="mp3", choices=["mp3", "m4a"])
    parser.add_argument("--lufs", type=float, default=-16)
    parser.add_argument("--sample-rate", type=int, default=48000, help="中間・出力サンプルレート")
    args = parser.parse_args()

    with open(args.metadata, encoding="utf-8") as f:
        metadata = json.load(f)
    with open(args.cuts, encoding="utf-8") as f:
        cuts = json.load(f)

    crosstalk_data = []
    if args.crosstalk and Path(args.crosstalk).exists():
        with open(args.crosstalk, encoding="utf-8") as f:
            ct = json.load(f)
        crosstalk_data = ct.get("overlaps", [])

    keep_segments = cuts["keep_segments"]
    out_path = Path(args.output)
    out_path.mkdir(parents=True, exist_ok=True)
    tracks_out = out_path / "tracks"
    tracks_out.mkdir(exist_ok=True)

    mode = metadata["mode"]
    tracks = metadata["tracks"]
    sr = args.sample_rate

    # metadata.jsonの親ディレクトリ（パス解決用）
    meta_dir = Path(args.metadata).parent

    # 高音質トラックディレクトリが指定されている場合、そこからソースを探す
    hq_tracks_dir = Path(args.tracks_dir) if args.tracks_dir else None

    def resolve_track_source(track_info):
        """トラックのソースWAVを解決"""
        speaker = track_info["speaker"]
        # 1. 高音質ディレクトリから探す
        if hq_tracks_dir and hq_tracks_dir.exists():
            for wav in hq_tracks_dir.glob("*.wav"):
                if speaker in wav.name or track_info.get("stream_index", -1) == int(wav.name.split("_")[1]) if "_" in wav.name else False:
                    return str(wav)
            # ファイル一覧から最初にマッチするものを返す
            wavs = sorted(hq_tracks_dir.glob("*.wav"))
            idx = metadata["speakers"].index(speaker) if speaker in metadata["speakers"] else -1
            if 0 <= idx < len(wavs):
                return str(wavs[idx])
        # 2. metadata記載パスから探す
        track_file = track_info["path"]
        p = Path(track_file).resolve()
        if p.exists():
            return str(p)
        p = (meta_dir / track_file).resolve()
        if p.exists():
            return str(p)
        return None

    if mode == "single":
        source = resolve_track_source(tracks[0])
        if not source:
            print(f"[ERROR] トラックファイルが見つかりません", file=sys.stderr)
            sys.exit(1)
        cut_path = tracks_out / "cut_mixed.wav"
        apply_cuts_to_track(source, keep_segments, str(cut_path))
        mixed_path = str(cut_path)
        print("[INFO] シングルトラック: カット適用完了")

    else:
        processed_tracks = []

        for track in tracks:
            speaker = track["speaker"]
            source = resolve_track_source(track)
            if not source:
                print(f"[ERROR] トラックファイルが見つかりません: {speaker}", file=sys.stderr)
                sys.exit(1)
            print(f"[INFO] トラック処理: {speaker} ({source})")

            # 1. カット適用
            cut_path = tracks_out / f"cut_{speaker}.wav"
            apply_cuts_to_track(source, keep_segments, str(cut_path))

            # 2. クロストーク処理
            ct_path = tracks_out / f"ct_{speaker}.wav"
            apply_crosstalk_processing(str(cut_path), speaker, crosstalk_data, str(ct_path))

            processed_tracks.append(str(ct_path))
            print(f"[INFO]   -> カット + クロストーク処理完了")

        # 3. ミックスダウン
        mixed_path = str(tracks_out / "mixed.wav")
        mixdown_tracks(processed_tracks, mixed_path)
        print(f"[INFO] ミックスダウン完了: {len(processed_tracks)}トラック")

    # 4. LUFS正規化 + エンコード
    ext = args.format
    final_path = out_path / f"output.{ext}"
    normalize_and_encode(mixed_path, str(final_path), args.lufs, args.format)

    # 出力情報
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(final_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    probe = json.loads(result.stdout)
    output_duration = float(probe["format"].get("duration", 0))

    ct_mute = sum(1 for c in crosstalk_data if c.get("action") == "mute")
    ct_duck = sum(1 for c in crosstalk_data if c.get("action") == "ducking")

    print(f"[INFO] 書き出し完了: {final_path}")
    print(f"[INFO] 出力: {output_duration:.1f}秒, {ext.upper()}, LUFS {args.lufs}")
    if crosstalk_data:
        print(f"[INFO] クロストーク処理: ミュート{ct_mute}件, ダッキング{ct_duck}件")


if __name__ == "__main__":
    main()
