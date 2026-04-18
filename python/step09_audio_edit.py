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

# OS共通の一時ディレクトリ（/tmp, %TEMP% 等）
_TMPDIR = Path(tempfile.gettempdir())


def merge_adjacent_segments(segments: list[dict], gap_threshold: float = 0.0) -> list[dict]:
    """gap_threshold 以下の間隔で連続するセグメントを結合する

    ゼロギャップの隣接セグメントを結合することで、同一点での
    fade-out/fade-in の二重適用による音量ディップを防ぐ。
    """
    if not segments:
        return segments
    merged = [dict(segments[0])]
    for seg in segments[1:]:
        gap = seg["start"] - merged[-1]["end"]
        if gap <= gap_threshold:
            merged[-1]["end"] = seg["end"]
            merged[-1]["duration"] = merged[-1]["end"] - merged[-1]["start"]
        else:
            merged.append(dict(seg))
    return merged


def apply_cuts_to_track(input_path: str, keep_segments: list[dict], output_path: str,
                        fade_ms: float = 50) -> None:
    """1トラックにカット提案を適用（バッチ処理対応）"""
    if not keep_segments:
        print(f"[WARN] keep_segmentsが空: {input_path}", file=sys.stderr)
        return

    # ゼロギャップの隣接セグメントをマージして二重フェードを防ぐ
    keep_segments = merge_adjacent_segments(keep_segments, gap_threshold=0.0)

    n = len(keep_segments)
    BATCH_SIZE = 200  # ffmpegのfilter_complex制限回避

    if n <= BATCH_SIZE:
        _apply_cuts_single(input_path, keep_segments, output_path, fade_ms=fade_ms)
    else:
        # バッチに分割して処理 → 最後に結合
        batch_files = []
        out_dir = Path(output_path).parent
        for batch_idx in range(0, n, BATCH_SIZE):
            batch_segs = keep_segments[batch_idx:batch_idx + BATCH_SIZE]
            batch_path = str(out_dir / f"_batch_{batch_idx}.wav")
            _apply_cuts_single(input_path, batch_segs, batch_path, fade_ms=fade_ms)
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


def _apply_cuts_single(input_path: str, keep_segments: list[dict], output_path: str,
                        fade_ms: float = 50) -> None:
    """1バッチ分のカット適用（接合部にフェードを付けてクリックノイズを防止）"""
    n = len(keep_segments)
    fade_d = fade_ms / 1000  # 秒

    filter_parts = []
    for i, seg in enumerate(keep_segments):
        dur = seg['end'] - seg['start']
        # セグメントが短すぎる場合はフェードを縮小（1/4以内）
        f = min(fade_d, dur / 4)
        filter_parts.append(
            f"[0:a]atrim=start={seg['start']}:end={seg['end']},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={f:.4f},"
            f"afade=t=out:st={max(0, dur - f):.4f}:d={f:.4f}[s{i}]"
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

    複数の volume= フィルターを連結してスクリプトファイル経由で渡す。
    （コマンドライン長制限を回避し、Windows/macOS/Linux 共通で動作）
    """
    actions = [ct for ct in crosstalk
               if ct.get("target_speaker") == speaker and ct["action"] in ("ducking", "mute")]

    if not actions:
        shutil.copy2(track_path, output_path)
        return

    filter_parts = []
    for act in actions:
        s, e = act["start"], act["end"]
        if act["action"] == "mute":
            vol = 0.0
        else:
            db = act.get("ducking_db", -12)
            vol = 10 ** (db / 20)
        filter_parts.append(f"volume=enable='between(t,{s},{e})':volume={vol:.4f}")

    if not filter_parts:
        shutil.copy2(track_path, output_path)
        return

    # フィルタースクリプトファイル経由（コマンドライン長制限回避・Windows互換）
    filter_str = f"[0:a]{','.join(filter_parts)}[out]"
    filter_path = str(Path(output_path).parent / f"_ct_{speaker}_filter.txt")
    with open(filter_path, "w", encoding="utf-8") as f:
        f.write(filter_str)

    cmd = [
        "ffmpeg", "-y", "-i", str(track_path),
        "-filter_complex_script", filter_path,
        "-map", "[out]",
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    os.remove(filter_path)
    if result.returncode != 0:
        print(f"[ERROR] クロストーク処理失敗 ({speaker}): {result.stderr[-400:]}", file=sys.stderr)
        sys.exit(1)


def measure_lufs(track_path: str) -> float | None:
    """ffmpeg loudnorm でトラックの integrated LUFS を測定"""
    cmd = [
        "ffmpeg", "-i", str(track_path),
        "-af", "loudnorm=I=-23:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", os.devnull
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    stats = parse_loudnorm_stats(result.stderr)
    if stats:
        try:
            return float(stats["input_i"])
        except (KeyError, ValueError):
            return None
    return None


def mixdown_tracks(track_paths: list[str], output_path: str) -> None:
    """複数トラックをミックスダウン（per-track LUFS 正規化後に合成）"""
    if len(track_paths) == 1:
        shutil.copy2(track_paths[0], output_path)
        return

    n = len(track_paths)
    inputs = []
    for t in track_paths:
        inputs += ["-i", t]

    # 各トラックの LUFS を測定し -23 LUFS を目標にゲイン補正
    # → ミックス後の LUFS 正規化（-16）と独立して、まず話者間レベルを揃える
    _TARGET_LUFS = -23.0
    gains = []
    for t in track_paths:
        lufs = measure_lufs(t)
        if lufs is not None and lufs > -70.0:
            gain_db = _TARGET_LUFS - lufs
            gains.append(10 ** (gain_db / 20))
            print(f"[INFO]   LUFS測定: {lufs:.1f} LUFS → gain {gain_db:+.1f}dB ({Path(t).name})")
        else:
            gains.append(0.7079)  # 測定失敗時フォールバック -3dB
            print(f"[WARN]   LUFS測定失敗、-3dB フォールバック ({Path(t).name})")

    # per-track ゲイン補正後にミックス
    filter_parts = []
    for i, gain in enumerate(gains):
        filter_parts.append(f"[{i}:a]volume={gain:.4f}[s{i}]")
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
    elif fmt == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
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


def generate_preview(track_paths: list[str], output_path: str, speed: float = 2.0) -> None:
    """複数トラックをミックスして指定倍速のプレビューWAVを生成"""
    n = len(track_paths)
    inputs = []
    for t in track_paths:
        inputs += ["-i", t]

    vol = 0.7079  # -3dB
    filter_parts = [f"[{i}:a]volume={vol}[s{i}]" for i in range(n)]
    mix_label = "".join(f"[s{i}]" for i in range(n))
    filter_parts.append(f"{mix_label}amix=inputs={n}:duration=longest:normalize=0,atempo={speed}[out]")
    filter_str = ";\n".join(filter_parts)

    filter_path = str(Path(output_path).parent / "_preview_filter.txt")
    with open(filter_path, "w", encoding="utf-8") as f:
        f.write(filter_str)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex_script", filter_path,
        "-map", "[out]",
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    os.remove(filter_path)
    if result.returncode != 0:
        print(f"[WARN] プレビュー生成失敗: {result.stderr[:300]}", file=sys.stderr)
        return
    size_mb = Path(output_path).stat().st_size / 1024 ** 2
    print(f"[INFO] プレビュー出力: {output_path} ({size_mb:.0f}MB, {speed}x速)")


def main():
    parser = argparse.ArgumentParser(description="音声編集 + クロストーク処理 + ラウドネス正規化")
    parser.add_argument("--metadata", required=True, help="step01のmetadata.json")
    parser.add_argument("--cuts", required=True, help="cut_proposal.json")
    parser.add_argument("--crosstalk", default=None, help="crosstalk_result.json")
    parser.add_argument("--tracks-dir", default=None, help="高音質トラックディレクトリ（48kHz WAV等）")
    parser.add_argument("--output", required=True, help="出力ディレクトリ")
    parser.add_argument("--format", default="mp3", choices=["mp3", "m4a", "wav"])
    parser.add_argument("--lufs", type=float, default=-16)
    parser.add_argument("--sample-rate", type=int, default=48000, help="中間・出力サンプルレート")
    parser.add_argument("--two-track", action="store_true",
                        help="ミックスダウンせず、トラック別に個別出力する（手動編集用）")
    parser.add_argument("--preview", action="store_true",
                        help="2倍速プレビューWAVを /tmp/{run_id}_preview_2x.wav に出力する")
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
    crossfade_ms = cuts.get("crossfade_ms", 50)
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
        # 1. metadata の source フィールド（元ファイル）を優先
        source_file = track_info.get("source")
        if source_file:
            p = Path(source_file).resolve()
            if p.exists():
                return str(p)
        # 2. 高音質ディレクトリが指定されている場合、インデックス順でマッチ
        if hq_tracks_dir and hq_tracks_dir.exists():
            wavs = sorted(hq_tracks_dir.glob("*.wav"))
            idx = metadata["speakers"].index(speaker) if speaker in metadata["speakers"] else -1
            if 0 <= idx < len(wavs):
                return str(wavs[idx])
        # 3. metadata記載パス（step01の16kHz変換済みトラック）
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
        apply_cuts_to_track(source, keep_segments, str(cut_path), fade_ms=crossfade_ms)
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
            apply_cuts_to_track(source, keep_segments, str(cut_path), fade_ms=crossfade_ms)

            # 2. クロストーク処理
            ct_path = tracks_out / f"ct_{speaker}.wav"
            apply_crosstalk_processing(str(cut_path), speaker, crosstalk_data, str(ct_path))

            processed_tracks.append(str(ct_path))
            print(f"[INFO]   -> カット + クロストーク処理完了")

        if args.two_track:
            # 2トラック個別出力: ミックスダウンせず各トラックを個別に正規化・エンコード
            ext = args.format
            final_paths = []
            for i, (track_info, ct_path) in enumerate(zip(tracks, processed_tracks)):
                speaker = track_info["speaker"]
                final_path = out_path / f"track_{i+1:02d}_{speaker}.{ext}"
                normalize_and_encode(ct_path, str(final_path), args.lufs, args.format)
                final_paths.append(str(final_path))
                print(f"[INFO] 2トラック出力 [{i+1}/{len(tracks)}]: {final_path}")
            # プレビュー生成
            if args.preview:
                run_id = Path(args.output).parent.name
                preview_path = str(_TMPDIR / f"{run_id}_preview_2x.wav")
                generate_preview(processed_tracks, preview_path)
            # 出力情報表示して終了
            ct_mute = sum(1 for c in crosstalk_data if c.get("action") == "mute")
            ct_duck = sum(1 for c in crosstalk_data if c.get("action") == "ducking")
            print(f"[INFO] 2トラック出力完了: {len(tracks)}ファイル, LUFS {args.lufs}")
            if crosstalk_data:
                print(f"[INFO] クロストーク処理: ミュート{ct_mute}件, ダッキング{ct_duck}件")
            return

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

    # プレビュー生成（シングルミックス or マルチトラック問わず）
    if args.preview:
        run_id = Path(args.output).parent.name
        preview_path = str(_TMPDIR / f"{run_id}_preview_2x.wav")
        preview_sources = processed_tracks if mode != "single" else [mixed_path]
        generate_preview(preview_sources, preview_path)


if __name__ == "__main__":
    main()
