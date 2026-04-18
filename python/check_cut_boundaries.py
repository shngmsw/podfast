"""カット境界 × 単語境界 照合スクリプト

STT の word-level タイムスタンプとカット提案を突き合わせ、
カットが単語の途中に入っていないか（= 途切れの原因）を検出する。

使い方:
  python check_cut_boundaries.py \\
    --stt runs/20260416_135600/step02_stt/stt_result.json \\
    --cuts runs/test_2min/step08_cut_proposal/cut_proposal.json \\
    --stt-offset 300 \\
    --fade-ms 50
"""

import argparse
import json


MAX_WORD_DURATION = 1.5  # これより長い単語はWhisperXのアライメントエラーとして除外


_EPSILON = 0.002  # 2ms: 浮動小数点誤差・スナップ境界の許容幅


def classify_cut(cut_t: float, words: list[dict], fade_s: float) -> list[dict]:
    """1つのカット境界時刻について、関連する単語を分類して返す"""
    issues = []
    for w in words:
        if (w["end"] - w["start"]) > MAX_WORD_DURATION:
            continue  # アライメントエラー除外
        ws, we = w["start"], w["end"]
        if ws >= we:
            continue
        if ws + _EPSILON < cut_t < we - _EPSILON:
            # カットが単語の途中に刺さっている
            ratio = (cut_t - ws) / (we - ws)
            issues.append({
                "type": "mid-word",
                "word": w["text"],
                "speaker": w.get("speaker", "?"),
                "word_start": ws,
                "word_end": we,
                "cut_t": cut_t,
                "cut_ratio": round(ratio, 2),
            })
        elif cut_t - fade_s < we <= cut_t:
            # フェードアウト区間に語尾が入る
            issues.append({
                "type": "tail-risk",
                "word": w["text"],
                "speaker": w.get("speaker", "?"),
                "word_start": ws,
                "word_end": we,
                "cut_t": cut_t,
                "gap_ms": round((cut_t - we) * 1000),
            })
        elif cut_t <= ws < cut_t + fade_s:
            # フェードイン区間に語頭が入る
            issues.append({
                "type": "head-risk",
                "word": w["text"],
                "speaker": w.get("speaker", "?"),
                "word_start": ws,
                "word_end": we,
                "cut_t": cut_t,
                "gap_ms": round((ws - cut_t) * 1000),
            })
    return issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stt", required=True, help="stt_result.json")
    parser.add_argument("--cuts", required=True, help="cut_proposal.json")
    parser.add_argument("--stt-offset", type=float, default=0.0,
                        help="STT の絶対時刻 → cuts の相対時刻への変換オフセット（秒）")
    parser.add_argument("--fade-ms", type=float, default=50,
                        help="フェード長（ms）。この範囲内の単語境界を risk として検出")
    args = parser.parse_args()

    with open(args.stt, encoding="utf-8") as f:
        stt = json.load(f)
    with open(args.cuts, encoding="utf-8") as f:
        cuts = json.load(f)

    fade_s = args.fade_ms / 1000
    offset = args.stt_offset

    # STT 単語データを cuts の時間軸に変換
    raw_words = stt.get("words", [])
    segs = cuts["keep_segments"]

    # cuts の時間範囲（相対）に対応する STT 単語を抽出し、オフセット補正
    if segs:
        cuts_start = segs[0]["start"]
        cuts_end = segs[-1]["end"]
    else:
        print("[ERROR] keep_segments が空です")
        return

    words = []
    for w in raw_words:
        ws = w.get("start", 0) - offset
        we = w.get("end", 0) - offset
        if cuts_start - 1.0 <= ws and we <= cuts_end + 1.0:
            words.append({**w, "start": ws, "end": we})

    print(f"対象単語数: {len(words)}語 (STT offset={offset}s, 範囲 {cuts_start:.1f}-{cuts_end:.1f}s)")
    print()

    # カット境界ごとに分析
    # カット境界 = keep_segment の end（最後を除く）
    results = []
    for seg in segs[:-1]:
        cut_t = seg["end"]
        issues = classify_cut(cut_t, words, fade_s)
        results.append({
            "cut_t": cut_t,
            "issues": issues,
            "status": "mid-word" if any(i["type"] == "mid-word" for i in issues)
                      else "risky" if issues
                      else "clean",
        })

    # 集計
    total = len(results)
    mid_word = [r for r in results if r["status"] == "mid-word"]
    risky = [r for r in results if r["status"] == "risky"]
    clean = [r for r in results if r["status"] == "clean"]

    print("=" * 50)
    print("カット境界 × 単語境界 分析")
    print("=" * 50)
    print(f"総カット境界数 : {total}件")
    print(f"  clean      : {len(clean)}件 ({len(clean)/total*100:.0f}%)")
    print(f"  risky      : {len(risky)}件 ({len(risky)/total*100:.0f}%)  ← フェード内に単語境界")
    print(f"  mid-word   : {len(mid_word)}件 ({len(mid_word)/total*100:.0f}%)  ← 単語の途中でカット")
    print()

    if mid_word:
        print("[mid-word カット詳細]")
        for r in mid_word:
            for iss in r["issues"]:
                if iss["type"] == "mid-word":
                    ws, we, ct = iss["word_start"], iss["word_end"], iss["cut_t"]
                    ratio = iss["cut_ratio"]
                    # カット点で単語を分割表示
                    word = iss["word"]
                    split_at = max(1, int(len(word) * ratio))
                    before = word[:split_at]
                    after = word[split_at:]
                    print(f"  t={ct:.3f}s  {iss['speaker']}: \"{before}|{after}\""
                          f"  (word: {ws:.3f}-{we:.3f}s, カット位置 {ratio*100:.0f}%)")
        print()

    if risky:
        print("[risky カット詳細]")
        for r in risky:
            for iss in r["issues"]:
                if iss["type"] == "tail-risk":
                    print(f"  t={iss['cut_t']:.3f}s  {iss['speaker']}: \"{iss['word']}\""
                          f"  語尾まで {iss['gap_ms']}ms (fade内)")
                elif iss["type"] == "head-risk":
                    print(f"  t={iss['cut_t']:.3f}s  {iss['speaker']}: \"{iss['word']}\""
                          f"  語頭から {iss['gap_ms']}ms (fade内)")
        print()

    # 診断コメント
    print("[診断]")
    if len(mid_word) == 0 and len(risky) == 0:
        print("  全カットが単語間 → 途切れの原因はカット境界ではなく別要因（fade設定・クロストーク等）")
    elif len(mid_word) > total * 0.1:
        print(f"  mid-word カットが {len(mid_word)/total*100:.0f}% → cut_proposal の境界が単語を跨いでいる")
        print("  → step08_cut_proposal でカット境界を単語末尾にスナップする改善が有効")
    else:
        print(f"  mid-word カットは少数 ({len(mid_word)}件)")
        if risky:
            print(f"  risky が {len(risky)}件 → fade_ms={args.fade_ms}ms を短くするか、境界を単語末尾にずらす")


if __name__ == "__main__":
    main()
