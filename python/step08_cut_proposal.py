"""Step 08: カット提案統合 - 全ステップの結果をマージしてkeep_segmentsを生成"""

import argparse
import json
from pathlib import Path


def load_words(stt_path: str) -> list[dict]:
    """STT結果から単語リストを取得"""
    with open(stt_path, encoding="utf-8") as f:
        stt = json.load(f)
    return [w for w in stt.get("words", []) if w.get("start") is not None and w.get("end") is not None]


def snap_to_word_boundary(t: float, words: list[dict], direction: str, snap_window: float,
                           max_word_duration: float = 1.5) -> float:
    """カット境界を単語末尾にスナップする

    カット境界が単語の途中に入っている場合のみスナップする。
    「途中に入っている」 = その単語の end が t + snap_window 以内にある。

    direction='left' : keep_segment の末端（カット開始点）
                       t が単語途中 → その単語の word_end までスナップ（末尾まで残す）
    direction='right': keep_segment の先頭（カット終了点）
                       t が単語途中 → その単語の word_end までスナップ（先頭を送らせる）
    snap_window       : word_end が t から最大この秒数以内ならスナップ
    max_word_duration : これより長い単語はアライメントエラーとして除外
    """
    for w in words:
        if (w["end"] - w["start"]) > max_word_duration:
            continue
        ws, we = w["start"], w["end"]
        # t が単語の途中にある（word_start < t < word_end）
        if ws < t < we:
            # word_end まで延ばす（snap_window 以内のみ）
            if we - t <= snap_window:
                return we
    return t


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


def compute_keep_segments(total_duration: float, cut_regions: list[dict],
                           min_segment_ms: int = 300, pre_roll_ms: int = 0,
                           words: list[dict] | None = None,
                           word_snap_ms: float = 300) -> list[dict]:
    """カット区間の補集合 = 残す区間を算出

    pre_roll_ms  : 各セグメントの開始をN ms手前に延ばす（話し始めの音が切れる場合に使用）
    words        : STT単語リスト。指定時はカット境界を最近傍の単語境界にスナップする
    word_snap_ms : スナップの最大許容距離（ms）。これより遠い単語境界はスナップしない
    """
    pre_roll = pre_roll_ms / 1000
    snap_window = word_snap_ms / 1000

    # カット区間をマージ（重複解消）
    merged_cuts = []
    for region in cut_regions:
        if merged_cuts and region["start"] <= merged_cuts[-1]["end"]:
            merged_cuts[-1]["end"] = max(merged_cuts[-1]["end"], region["end"])
        else:
            merged_cuts.append({"start": region["start"], "end": region["end"]})

    # 残す区間を算出
    keep_segments = []
    prev_keep_end = 0.0
    prev_cut_end = 0.0
    snapped_count = 0

    for cut in merged_cuts:
        seg_start = max(prev_keep_end, prev_cut_end - pre_roll)
        seg_end = cut["start"]

        # 単語境界スナップ
        if words:
            orig_start, orig_end = seg_start, seg_end
            seg_end = snap_to_word_boundary(seg_end, words, "left", snap_window)
            seg_start = snap_to_word_boundary(seg_start, words, "right", snap_window)
            if seg_end != orig_end or seg_start != orig_start:
                snapped_count += 1

        if seg_end - seg_start >= min_segment_ms / 1000:
            keep_segments.append({
                "start": round(seg_start, 3),
                "end": round(seg_end, 3),
                "duration": round(seg_end - seg_start, 3),
            })
            prev_keep_end = seg_end
        prev_cut_end = cut["end"]

    # 末尾
    seg_start = max(prev_keep_end, prev_cut_end - pre_roll)
    if words:
        seg_start = snap_to_word_boundary(seg_start, words, "right", snap_window)
    if total_duration - seg_start >= min_segment_ms / 1000:
        keep_segments.append({
            "start": round(seg_start, 3),
            "end": round(total_duration, 3),
            "duration": round(total_duration - seg_start, 3),
        })

    if words and snapped_count:
        print(f"[INFO] 単語境界スナップ: {snapped_count}件のセグメント境界を調整")

    return keep_segments


def main():
    parser = argparse.ArgumentParser(description="カット提案統合")
    parser.add_argument("--vad", required=True)
    parser.add_argument("--filler", required=True)
    parser.add_argument("--retake", required=True)
    parser.add_argument("--metadata", required=True, help="step01のmetadata.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--pre-roll-ms", type=int, default=0,
                        help="各セグメント開始をN ms手前に延ばす（話し始めの音切れ対策）")
    parser.add_argument("--stt", default=None,
                        help="stt_result.json。指定するとカット境界を単語末尾にスナップする")
    parser.add_argument("--word-snap-ms", type=float, default=1000,
                        help="単語境界スナップの最大許容距離（ms）。デフォルト1000ms")
    args = parser.parse_args()

    # メタデータから総時間取得
    with open(args.metadata, encoding="utf-8") as f:
        meta = json.load(f)
    total_duration = meta["duration_sec"]

    # カット区間をマージ
    cut_regions = merge_cut_segments(args.vad, args.filler, args.retake)

    # 単語リスト（スナップ用）
    words = None
    if args.stt and Path(args.stt).exists():
        words = load_words(args.stt)
        print(f"[INFO] STT単語データ読み込み: {len(words)}語")

    # keep_segments算出
    keep_segments = compute_keep_segments(total_duration, cut_regions,
                                          pre_roll_ms=args.pre_roll_ms,
                                          words=words,
                                          word_snap_ms=args.word_snap_ms)

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
