"""WhisperX 文字起こしのみ（話者分離なし）- HF_TOKEN 不要"""

import argparse
import json
import os
from pathlib import Path

import torch
import whisperx


def main():
    parser = argparse.ArgumentParser(description="WhisperX 文字起こし（話者分離なし）")
    parser.add_argument("--audio", required=True, help="16kHz WAV ファイルパス")
    parser.add_argument("--output", required=True, help="出力ディレクトリ")
    parser.add_argument("--model", default=os.getenv("WHISPER_MODEL", "large-v3"))
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"[INFO] デバイス: {device}, モデル: {args.model}, compute_type: {compute_type}")

    print("[INFO] 文字起こし開始...")
    model = whisperx.load_model(args.model, device, compute_type=compute_type, language="ja")
    audio = whisperx.load_audio(args.audio)
    result = model.transcribe(audio, batch_size=16 if device == "cuda" else 4)
    print(f"[INFO] セグメント数: {len(result['segments'])}")

    print("[INFO] アライメント開始...")
    model_a, align_metadata = whisperx.load_align_model(language_code="ja", device=device)
    result = whisperx.align(result["segments"], model_a, align_metadata, audio, device,
                            return_char_alignments=False)

    words = []
    segments = []
    for seg in result.get("segments", []):
        segments.append({
            "text": seg.get("text", ""),
            "start": round(seg.get("start", 0), 3),
            "end": round(seg.get("end", 0), 3),
            "speaker": "SPEAKER_00",
        })
        for w in seg.get("words", []):
            words.append({
                "text": w.get("word", ""),
                "start": round(w.get("start", 0), 3),
                "end": round(w.get("end", 0), 3),
                "speaker": "SPEAKER_00",
                "score": round(w.get("score", 0), 3),
            })

    output = {
        "words": words,
        "speakers": ["SPEAKER_00"],
        "num_speakers": 1,
        "total_words": len(words),
        "segments": segments,
    }

    out_path = Path(args.output)
    out_path.mkdir(parents=True, exist_ok=True)
    result_path = out_path / "stt_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 文字起こし完了: {len(words)}単語")
    print(f"[INFO] 保存先: {result_path}")


if __name__ == "__main__":
    main()
