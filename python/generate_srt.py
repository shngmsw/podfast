"""stt_result.json から SRT ファイルを生成 + 時系列マージした話者ラベル付きMarkdownを生成"""

import argparse
import json
from pathlib import Path


def fmt_srt_time(sec: float) -> str:
    if sec < 0:
        sec = 0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_readable_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_srt(segments, out_path: Path):
    lines = []
    for i, seg in enumerate(segments, 1):
        start = fmt_srt_time(seg.get("start", 0))
        end = fmt_srt_time(seg.get("end", 0))
        text = (seg.get("text") or "").strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, help="runs/{run_id}/ ディレクトリ")
    parser.add_argument("--speakers", required=True, help="カンマ区切り話者名（stt_{name}ディレクトリと対応）")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    speakers = [s.strip() for s in args.speakers.split(",")]

    all_segments = []
    for sp in speakers:
        stt_path = run_dir / f"stt_{sp}" / "stt_result.json"
        data = json.loads(stt_path.read_text(encoding="utf-8"))
        segs = data.get("segments", [])
        # 個別SRT
        srt_path = run_dir / f"{sp}.srt"
        write_srt(segs, srt_path)
        print(f"[INFO] {sp}: {len(segs)} segments -> {srt_path.name}")
        for s in segs:
            all_segments.append({**s, "speaker": sp})

    # 時系列マージ
    all_segments.sort(key=lambda s: s.get("start", 0))

    # マージSRT（話者名をテキスト先頭に付与）
    merged_srt_segs = []
    for s in all_segments:
        merged_srt_segs.append({
            "start": s.get("start", 0),
            "end": s.get("end", 0),
            "text": f"[{s['speaker']}] {(s.get('text') or '').strip()}",
        })
    merged_srt = run_dir / "merged.srt"
    write_srt(merged_srt_segs, merged_srt)
    print(f"[INFO] マージSRT: {len(all_segments)} segments -> {merged_srt.name}")

    # 読みやすい Markdown トランスクリプト
    md_lines = ["# トランスクリプト（時系列）\n"]
    last_speaker = None
    for s in all_segments:
        t = fmt_readable_time(s.get("start", 0))
        sp = s["speaker"]
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if sp != last_speaker:
            md_lines.append(f"\n**[{t}] {sp}**")
            last_speaker = sp
        md_lines.append(f"- {text}  `({t})`")

    md_path = run_dir / "transcript.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"[INFO] マージMarkdown -> {md_path.name}")


if __name__ == "__main__":
    main()
