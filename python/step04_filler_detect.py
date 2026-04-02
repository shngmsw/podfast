"""Step 04: フィラー検出 (29パターン辞書マッチング)"""

import argparse
import json
import re
from pathlib import Path

# フィラーパターン辞書（29パターン）
# exact: 完全一致、prefix: 前方一致
FILLER_PATTERNS = [
    # 典型的フィラー
    {"pattern": "えー", "type": "exact"},
    {"pattern": "えーと", "type": "exact"},
    {"pattern": "えーっと", "type": "exact"},
    {"pattern": "えっと", "type": "exact"},
    {"pattern": "あー", "type": "exact"},
    {"pattern": "あのー", "type": "exact"},
    {"pattern": "あの", "type": "exact"},
    {"pattern": "そのー", "type": "exact"},
    {"pattern": "うーん", "type": "exact"},
    {"pattern": "うん", "type": "exact"},
    {"pattern": "んー", "type": "exact"},
    {"pattern": "まあ", "type": "exact"},
    {"pattern": "まぁ", "type": "exact"},
    {"pattern": "なんか", "type": "exact"},
    {"pattern": "こう", "type": "exact"},
    {"pattern": "ほら", "type": "exact"},
    {"pattern": "ねー", "type": "exact"},
    {"pattern": "ね", "type": "exact"},
    # 長音化バリエーション
    {"pattern": "えーーー", "type": "prefix"},
    {"pattern": "あーーー", "type": "prefix"},
    {"pattern": "うーーー", "type": "prefix"},
    {"pattern": "んーーー", "type": "prefix"},
    # ひらがな表記ゆれ
    {"pattern": "ええと", "type": "exact"},
    {"pattern": "ええっと", "type": "exact"},
    {"pattern": "あのね", "type": "exact"},
    {"pattern": "そうですね", "type": "exact"},
    {"pattern": "なんていうか", "type": "exact"},
    {"pattern": "なんというか", "type": "exact"},
    {"pattern": "いわゆる", "type": "exact"},
]


def is_filler(word_text: str) -> dict | None:
    """単語がフィラーパターンに一致するか判定"""
    text = word_text.strip()
    for p in FILLER_PATTERNS:
        if p["type"] == "exact" and text == p["pattern"]:
            return p
        elif p["type"] == "prefix" and text.startswith(p["pattern"][:3]):
            # 長音化パターン: 3文字以上の前方一致
            if len(text) >= 2 and all(c in text[1:] for c in ["ー", "ー"]):
                return p
    return None


def detect_fillers(stt_path: str, vad_path: str, output_dir: str):
    with open(stt_path, encoding="utf-8") as f:
        stt = json.load(f)

    words = stt["words"]
    segments = stt.get("segments", [])

    filler_segments = []

    for i, word in enumerate(words):
        match = is_filler(word["text"])
        if not match:
            continue

        # 文脈チェック: 文頭フィラーか文中フィラーか
        is_sentence_start = True
        if i > 0:
            prev = words[i - 1]
            # 同一話者で0.5秒以内に前の単語がある場合は文中
            if (prev["speaker"] == word["speaker"] and
                    word["start"] - prev["end"] < 0.5):
                is_sentence_start = False

        # 文中の「まあ」「なんか」「こう」は意味を持つことがあるので慎重に
        cautious_words = {"まあ", "まぁ", "なんか", "こう", "ほら", "ね", "いわゆる"}
        if word["text"] in cautious_words and not is_sentence_start:
            # 文中の場合はスキップ（filler_reviewでClaude Codeが判断）
            filler_segments.append({
                "start": word["start"],
                "end": word["end"],
                "text": word["text"],
                "speaker": word["speaker"],
                "pattern": match["pattern"],
                "position": "mid_sentence",
                "confidence": "low",
                "action": "review",  # Claude Codeにレビューを委ねる
            })
            continue

        filler_segments.append({
            "start": word["start"],
            "end": word["end"],
            "text": word["text"],
            "speaker": word["speaker"],
            "pattern": match["pattern"],
            "position": "sentence_start" if is_sentence_start else "mid_sentence",
            "confidence": "high",
            "action": "cut",
        })

    # 統計
    cut_count = sum(1 for f in filler_segments if f["action"] == "cut")
    review_count = sum(1 for f in filler_segments if f["action"] == "review")

    output = {
        "filler_segments": filler_segments,
        "stats": {
            "total_detected": len(filler_segments),
            "auto_cut": cut_count,
            "needs_review": review_count,
        }
    }

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    result_path = out_path / "filler_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] フィラー検出完了: {len(filler_segments)}件 (自動カット: {cut_count}, 要レビュー: {review_count})")
    print(f"[INFO] 保存先: {result_path}")


def main():
    parser = argparse.ArgumentParser(description="フィラー検出")
    parser.add_argument("--stt", required=True)
    parser.add_argument("--vad", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    detect_fillers(args.stt, args.vad, args.output)


if __name__ == "__main__":
    main()
