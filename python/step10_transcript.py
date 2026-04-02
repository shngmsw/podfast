"""Step 10: 話者ラベル付きMarkdownトランスクリプト生成"""

import argparse
import json
import re
from pathlib import Path


def format_timestamp(seconds: float) -> str:
    """秒をHH:MM:SS形式に変換"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def apply_corrections(text: str, corrections: list[dict]) -> str:
    """review.jsonの修正を適用"""
    for corr in corrections:
        text = text.replace(corr["original"], corr["corrected"])
    return text


def clean_punctuation(text: str) -> str:
    """句読点の正規化"""
    # 連続する句読点を1つに
    text = re.sub(r"[、。]{2,}", "。", text)
    # 文中の不要な句点を除去（文末以外）
    # 読点が多すぎる場合は間引く（3つ以上連続する節の場合）
    return text


def compute_edited_timestamp(original_time: float, keep_segments: list[dict]) -> float | None:
    """元の時間軸から編集後の時間軸に変換"""
    elapsed = 0.0
    for seg in keep_segments:
        if original_time < seg["start"]:
            return elapsed
        if seg["start"] <= original_time <= seg["end"]:
            return elapsed + (original_time - seg["start"])
        elapsed += seg["end"] - seg["start"]
    return elapsed


def generate_transcript(stt_path: str, cuts_path: str, review_path: str,
                        speaker_names: dict, output_dir: str):
    with open(stt_path, encoding="utf-8") as f:
        stt = json.load(f)
    with open(cuts_path, encoding="utf-8") as f:
        cuts = json.load(f)

    corrections = []
    if Path(review_path).exists():
        with open(review_path, encoding="utf-8") as f:
            review = json.load(f)
        corrections = review.get("corrections", [])

    keep_segments = cuts["keep_segments"]
    segments = stt["segments"]

    # セグメントをkeep_segments内のものだけフィルタ
    kept_segments = []
    for seg in segments:
        seg_mid = (seg["start"] + seg["end"]) / 2
        for ks in keep_segments:
            if ks["start"] <= seg_mid <= ks["end"]:
                kept_segments.append(seg)
                break

    # Markdownを構築
    lines = ["# トランスクリプト\n"]
    current_speaker = None
    current_block_start = None
    current_texts = []
    block_interval = 120  # 2分ごとにセクション区切り

    section_start = 0.0

    for seg in kept_segments:
        speaker_id = seg.get("speaker", "UNKNOWN")
        speaker_name = speaker_names.get(speaker_id, speaker_id)

        edited_time = compute_edited_timestamp(seg["start"], keep_segments)
        if edited_time is None:
            continue

        # 2分ごとにセクション見出し
        if edited_time - section_start >= block_interval:
            if current_texts:
                lines.append(f"**{current_speaker}:** {''.join(current_texts)}\n")
                current_texts = []
                current_speaker = None

            section_end_time = edited_time + block_interval
            lines.append(f"\n## {format_timestamp(edited_time)}\n")
            section_start = edited_time

        text = seg["text"].strip()
        text = apply_corrections(text, corrections)
        text = clean_punctuation(text)

        if speaker_name != current_speaker:
            # 話者交代
            if current_texts:
                lines.append(f"**{current_speaker}:** {''.join(current_texts)}\n")
            current_speaker = speaker_name
            current_texts = [text]
        else:
            current_texts.append(text)

    # 最後のブロック
    if current_texts and current_speaker:
        lines.append(f"**{current_speaker}:** {''.join(current_texts)}\n")

    # 書き出し
    transcript_md = "\n".join(lines)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    result_path = out_path / "transcript.md"
    with open(result_path, "w", encoding="utf-8") as f:
        f.write(transcript_md)

    print(f"[INFO] トランスクリプト生成完了: {len(kept_segments)}セグメント")
    print(f"[INFO] 保存先: {result_path}")


def main():
    parser = argparse.ArgumentParser(description="トランスクリプト生成")
    parser.add_argument("--stt", required=True)
    parser.add_argument("--cuts", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--speakers", default="", help='話者名マッピング: "SPEAKER_00=田中,SPEAKER_01=佐藤"')
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    # 話者名パース
    speaker_names = {}
    if args.speakers:
        for pair in args.speakers.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                speaker_names[k.strip()] = v.strip()

    generate_transcript(args.stt, args.cuts, args.review, speaker_names, args.output)


if __name__ == "__main__":
    main()
