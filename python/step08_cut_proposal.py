"""Step 08: カット提案統合 - 全ステップの結果をマージしてkeep_segmentsを生成"""

import argparse
import json
from pathlib import Path


def merge_cut_segments(vad_path: str, filler_path: str, retake_path: str) -> list[dict]:
    """無音・フィラー・言い直しのカット区間をマージ"""

    cut_regions = []

    # 1. 無音区間
    with open(vad_path, encoding="utf-8") as f:
        vad = json.load(f)
    for seg in vad["silence_segments"]:
        cut_regions.append({
            "start": seg["start"],
            "end": seg["end"],
            "reason": "silence",
        })

    # 2. フィラー（action=cutのみ）
    with open(filler_path, encoding="utf-8") as f:
        filler = json.load(f)
    for seg in filler["filler_segments"]:
        if seg["action"] == "cut":
            cut_regions.append({
                "start": seg["start"],
                "end": seg["end"],
                "reason": "filler",
            })

    # 3. 言い直し
    retake_file = Path(retake_path)
    if retake_file.exists():
        with open(retake_file, encoding="utf-8") as f:
            retake = json.load(f)
        for seg in retake.get("retakes", []):
            cut_regions.append({
                "start": seg["start"],
                "end": seg["end"],
                "reason": "retake",
            })

    # 時系列順にソート
    cut_regions.sort(key=lambda x: x["start"])
    return cut_regions


def compute_keep_segments(total_duration: float, cut_regions: list[dict], min_segment_ms: int = 300) -> list[dict]:
    """カット区間の補集合 = 残す区間を算出"""

    # カット区間をマージ（重複解消）
    merged_cuts = []
    for region in cut_regions:
        if merged_cuts and region["start"] <= merged_cuts[-1]["end"]:
            merged_cuts[-1]["end"] = max(merged_cuts[-1]["end"], region["end"])
        else:
            merged_cuts.append({"start": region["start"], "end": region["end"]})

    # 残す区間を算出
    keep_segments = []
    prev_end = 0.0
    for cut in merged_cuts:
        if cut["start"] - prev_end >= min_segment_ms / 1000:
            keep_segments.append({
                "start": round(prev_end, 3),
                "end": round(cut["start"], 3),
                "duration": round(cut["start"] - prev_end, 3),
            })
        prev_end = cut["end"]

    # 末尾
    if total_duration - prev_end >= min_segment_ms / 1000:
        keep_segments.append({
            "start": round(prev_end, 3),
            "end": round(total_duration, 3),
            "duration": round(total_duration - prev_end, 3),
        })

    return keep_segments


def main():
    parser = argparse.ArgumentParser(description="カット提案統合")
    parser.add_argument("--vad", required=True)
    parser.add_argument("--filler", required=True)
    parser.add_argument("--retake", required=True)
    parser.add_argument("--metadata", required=True, help="step01のmetadata.json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    # メタデータから総時間取得
    with open(args.metadata, encoding="utf-8") as f:
        meta = json.load(f)
    total_duration = meta["duration_sec"]

    # カット区間をマージ
    cut_regions = merge_cut_segments(args.vad, args.filler, args.retake)

    # keep_segments算出
    keep_segments = compute_keep_segments(total_duration, cut_regions)

    # 統計
    kept_duration = sum(s["duration"] for s in keep_segments)
    cut_duration = total_duration - kept_duration
    cut_ratio = cut_duration / total_duration if total_duration > 0 else 0

    # カット理由別の集計
    reason_counts = {}
    for r in cut_regions:
        reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1

    output = {
        "keep_segments": keep_segments,
        "cut_regions": cut_regions,
        "stats": {
            "original_duration": round(total_duration, 2),
            "kept_duration": round(kept_duration, 2),
            "cut_duration": round(cut_duration, 2),
            "cut_ratio": round(cut_ratio, 4),
            "num_keep_segments": len(keep_segments),
            "num_cuts": len(cut_regions),
            "cuts_by_reason": reason_counts,
        },
        "crossfade_ms": 50,
    }

    out_path = Path(args.output)
    out_path.mkdir(parents=True, exist_ok=True)
    result_path = out_path / "cut_proposal.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] カット提案完了:")
    print(f"  元音声: {total_duration:.1f}秒 → 編集後: {kept_duration:.1f}秒 (削減率: {cut_ratio:.1%})")
    print(f"  カット数: {len(cut_regions)}件 {reason_counts}")
    print(f"  セグメント数: {len(keep_segments)}")
    print(f"[INFO] 保存先: {result_path}")


if __name__ == "__main__":
    main()
