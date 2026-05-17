"""Step 04b: クロストーク検出 - 複数トラックのVAD重なり区間を検出し処理方針を決定

マルチトラック録音で発話が被っている区間を検出し、以下の処理を提案する:
- ducking: 相槌/笑い側の音量を下げる (-12dB)
- mute: 短い割り込み側を無音にする
- keep: 双方が実質的に発話しているのでそのまま残す
- review: Claude Codeに判断を委ねる
"""

import argparse
import json
from pathlib import Path


def find_overlaps(vad_tracks: list[dict], min_overlap_ms: float = 100) -> list[dict]:
    """複数トラックのVAD結果から発話重なり区間を検出

    Args:
        vad_tracks: [{"speaker": str, "speech_segments": [{"start","end"}]}]
        min_overlap_ms: 最小重なり長さ (ms)
    """
    if len(vad_tracks) < 2:
        return []

    overlaps = []

    # 全ペアで重なりチェック
    for i in range(len(vad_tracks)):
        for j in range(i + 1, len(vad_tracks)):
            segs_a = vad_tracks[i]["speech_segments"]
            segs_b = vad_tracks[j]["speech_segments"]
            speaker_a = vad_tracks[i]["speaker"]
            speaker_b = vad_tracks[j]["speaker"]

            # 2つのセグメントリストの重なりを探す
            idx_b = 0
            for seg_a in segs_a:
                while idx_b < len(segs_b) and segs_b[idx_b]["end"] <= seg_a["start"]:
                    idx_b += 1

                k = idx_b
                while k < len(segs_b) and segs_b[k]["start"] < seg_a["end"]:
                    # 重なり区間を計算
                    overlap_start = max(seg_a["start"], segs_b[k]["start"])
                    overlap_end = min(seg_a["end"], segs_b[k]["end"])
                    overlap_duration = overlap_end - overlap_start

                    if overlap_duration >= min_overlap_ms / 1000:
                        overlaps.append({
                            "start": round(overlap_start, 3),
                            "end": round(overlap_end, 3),
                            "duration": round(overlap_duration, 3),
                            "speakers": [speaker_a, speaker_b],
                            "seg_a": {"start": seg_a["start"], "end": seg_a["end"], "speaker": speaker_a},
                            "seg_b": {"start": segs_b[k]["start"], "end": segs_b[k]["end"], "speaker": speaker_b},
                        })
                    k += 1

    overlaps.sort(key=lambda x: x["start"])
    return overlaps


def classify_overlap(overlap: dict, stt_words: list[dict]) -> dict:
    """重なり区間の処理方針を分類

    判定ロジック:
    1. 重なりが1秒未満かつ片方の発話が3文字以内 → 短い方をducking
    2. 重なりが0.5秒未満 → 短い方をmute
    3. 片方の発話量が明らかに少ない → 少ない方をducking
    4. それ以外 → review (Claude Codeに委ねる)
    """
    ov_start = overlap["start"]
    ov_end = overlap["end"]
    duration = overlap["duration"]
    speaker_a = overlap["speakers"][0]
    speaker_b = overlap["speakers"][1]

    # 重なり区間内の各話者のテキストを集める
    text_a = ""
    text_b = ""
    for w in stt_words:
        if w["start"] >= ov_start and w["end"] <= ov_end:
            if w.get("speaker") == speaker_a:
                text_a += w["text"]
            elif w.get("speaker") == speaker_b:
                text_b += w["text"]

    len_a = len(text_a.strip())
    len_b = len(text_b.strip())

    # 相槌パターン
    aizuchi_patterns = {"うん", "うんうん", "はい", "ああ", "へえ", "そうそう",
                        "なるほど", "確かに", "ですね", "はいはい", "ふーん", "えー"}

    a_is_aizuchi = text_a.strip() in aizuchi_patterns or len_a <= 3
    b_is_aizuchi = text_b.strip() in aizuchi_patterns or len_b <= 3

    result = {**overlap, "text_a": text_a.strip(), "text_b": text_b.strip()}

    # 判定
    if duration < 0.5 and (a_is_aizuchi or b_is_aizuchi):
        # 短い相槌 → ミュート
        mute_speaker = speaker_a if a_is_aizuchi else speaker_b
        result["action"] = "mute"
        result["target_speaker"] = mute_speaker
        result["reason"] = f"短い相槌 ({mute_speaker}: '{text_a.strip() if mute_speaker == speaker_a else text_b.strip()}')"

    elif duration < 1.0 and (a_is_aizuchi or b_is_aizuchi):
        # 相槌だが少し長め → ダッキング
        duck_speaker = speaker_a if a_is_aizuchi else speaker_b
        result["action"] = "ducking"
        result["target_speaker"] = duck_speaker
        result["ducking_db"] = -8
        result["reason"] = f"相槌ダッキング ({duck_speaker})"

    elif duration < 1.5 and (len_a == 0 or len_b == 0):
        # 片方にSTTテキストがない（笑い/息等） → ダッキング
        duck_speaker = speaker_a if len_a <= len_b else speaker_b
        result["action"] = "ducking"
        result["target_speaker"] = duck_speaker
        result["ducking_db"] = -10
        result["reason"] = "非発話ダッキング"

    elif duration < 0.3:
        # 非常に短い被り → そのまま
        result["action"] = "keep"
        result["reason"] = "微小重なり"

    elif abs(len_a - len_b) > max(len_a, len_b) * 0.7 and max(len_a, len_b) > 5:
        # 片方の発話量が明らかに多い → 少ない方をダッキング
        duck_speaker = speaker_a if len_a < len_b else speaker_b
        result["action"] = "ducking"
        result["target_speaker"] = duck_speaker
        result["ducking_db"] = -6
        result["reason"] = f"発話量差 (A:{len_a}字 vs B:{len_b}字)"

    else:
        # Claude Codeに判断を委ねる
        result["action"] = "review"
        result["reason"] = f"双方発話 (A:{len_a}字, B:{len_b}字, {duration:.1f}秒)"

    return result


def main():
    parser = argparse.ArgumentParser(description="クロストーク検出")
    parser.add_argument("--vad-dir", required=True, help="トラック別VAD結果のディレクトリ")
    parser.add_argument("--stt", required=True, help="stt_result.json (全トラック統合)")
    parser.add_argument("--metadata", required=True, help="step01のmetadata.json")
    parser.add_argument("--output", required=True, help="出力ディレクトリ")
    args = parser.parse_args()

    with open(args.metadata, encoding="utf-8") as f:
        metadata = json.load(f)

    if metadata["mode"] == "single":
        print("[INFO] シングルトラックモード: クロストーク検出スキップ")
        out_path = Path(args.output)
        out_path.mkdir(parents=True, exist_ok=True)
        with open(out_path / "crosstalk_result.json", "w", encoding="utf-8") as f:
            json.dump({"overlaps": [], "stats": {"skipped": True, "reason": "single_track"}},
                      f, ensure_ascii=False, indent=2)
        return

    # トラック別VAD結果を読み込み
    vad_dir = Path(args.vad_dir)
    vad_tracks = []
    for track in metadata["tracks"]:
        speaker = track["speaker"]
        vad_file = vad_dir / f"vad_{speaker}.json"
        if vad_file.exists():
            with open(vad_file, encoding="utf-8") as f:
                vad_data = json.load(f)
            vad_tracks.append({
                "speaker": speaker,
                "speech_segments": vad_data["speech_segments"],
            })
        else:
            print(f"[WARN] VADファイルなし: {vad_file}")

    if len(vad_tracks) < 2:
        print("[WARN] 有効なトラックが2未満、クロストーク検出スキップ")
        return

    # STT結果読み込み
    with open(args.stt, encoding="utf-8") as f:
        stt = json.load(f)
    words = stt.get("words", [])

    # 重なり検出
    overlaps = find_overlaps(vad_tracks, min_overlap_ms=100)
    print(f"[INFO] 重なり検出: {len(overlaps)}区間")

    # 分類
    classified = [classify_overlap(ov, words) for ov in overlaps]

    # 統計
    action_counts = {}
    for c in classified:
        action_counts[c["action"]] = action_counts.get(c["action"], 0) + 1

    total_overlap_sec = sum(c["duration"] for c in classified)

    output = {
        "overlaps": classified,
        "stats": {
            "total_overlaps": len(classified),
            "total_overlap_duration": round(total_overlap_sec, 2),
            "actions": action_counts,
        }
    }

    out_path = Path(args.output)
    out_path.mkdir(parents=True, exist_ok=True)
    result_path = out_path / "crosstalk_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] クロストーク処理方針: {action_counts}")
    print(f"[INFO] 合計重なり時間: {total_overlap_sec:.1f}秒")
    print(f"[INFO] 保存先: {result_path}")


if __name__ == "__main__":
    main()
