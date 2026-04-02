"""Step 02: WhisperX word-level文字起こし + pyannote話者分離"""

import argparse
import json
import os
import sys
from pathlib import Path

import torch
import whisperx


def load_env():
    """環境変数を読み込み"""
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def transcribe_track(model, model_a, align_metadata, audio_path: str, speaker: str, device: str):
    """1トラックを文字起こし（話者分離なし）"""
    print(f"[INFO] トラック文字起こし開始: {speaker} ({audio_path})")
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio, batch_size=16 if device == "cuda" else 4)
    print(f"[INFO] {speaker}: セグメント数 {len(result['segments'])}")

    result = whisperx.align(result["segments"], model_a, align_metadata, audio, device, return_char_alignments=False)

    words = []
    segments = []
    for seg in result.get("segments", []):
        segments.append({
            "text": seg.get("text", ""),
            "start": round(seg.get("start", 0), 3),
            "end": round(seg.get("end", 0), 3),
            "speaker": speaker,
        })
        for w in seg.get("words", []):
            words.append({
                "text": w.get("word", ""),
                "start": round(w.get("start", 0), 3),
                "end": round(w.get("end", 0), 3),
                "speaker": speaker,
                "score": round(w.get("score", 0), 3),
            })
    print(f"[INFO] {speaker}: {len(words)}単語")
    return words, segments


def run_stt_multitrack(metadata_path: str, output_dir: str):
    """マルチトラックモード: 各トラックを個別に文字起こしして統合"""
    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    model_name = os.getenv("WHISPER_MODEL", "large-v3")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"[INFO] マルチトラックSTT: {len(metadata['tracks'])}トラック")
    print(f"[INFO] デバイス: {device}, モデル: {model_name}, compute_type: {compute_type}")

    model = whisperx.load_model(model_name, device, compute_type=compute_type, language="ja")
    model_a, align_meta = whisperx.load_align_model(language_code="ja", device=device)

    all_words = []
    all_segments = []

    for track in metadata["tracks"]:
        raw_path = track["path"]
        # パスがcwd（python/）からの相対パスとして記録されている
        track_path = Path(raw_path).resolve()
        if not track_path.exists():
            # metadata.jsonの親ディレクトリからの相対パスも試行
            track_path = (Path(metadata_path).parent / raw_path).resolve()
        if not track_path.exists():
            print(f"[ERROR] トラックファイルが見つかりません: {raw_path}", file=sys.stderr)
            sys.exit(1)
        speaker = track["speaker"]
        words, segments = transcribe_track(model, model_a, align_meta, str(track_path), speaker, device)
        all_words.extend(words)
        all_segments.extend(segments)

    all_words.sort(key=lambda w: w["start"])
    all_segments.sort(key=lambda s: s["start"])

    speakers = metadata["speakers"]
    output = {
        "words": all_words,
        "speakers": speakers,
        "num_speakers": len(speakers),
        "total_words": len(all_words),
        "segments": all_segments,
    }

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    result_path = out_path / "stt_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 文字起こし完了: {len(all_words)}単語, {len(speakers)}話者")
    print(f"[INFO] 話者: {', '.join(speakers)}")
    print(f"[INFO] 保存先: {result_path}")


def run_stt(audio_path: str, output_dir: str, num_speakers: int | None = None):
    """シングルトラックモード: 話者分離付き"""
    load_env()

    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("[ERROR] HF_TOKEN未設定。.envファイルを確認してください。", file=sys.stderr)
        sys.exit(1)

    model_name = os.getenv("WHISPER_MODEL", "large-v3")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    print(f"[INFO] デバイス: {device}, モデル: {model_name}, compute_type: {compute_type}")

    print("[INFO] 文字起こし開始...")
    model = whisperx.load_model(model_name, device, compute_type=compute_type, language="ja")
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio, batch_size=16 if device == "cuda" else 4)
    print(f"[INFO] セグメント数: {len(result['segments'])}")

    print("[INFO] アライメント開始...")
    model_a, metadata = whisperx.load_align_model(language_code="ja", device=device)
    result = whisperx.align(result["segments"], model_a, metadata, audio, device, return_char_alignments=False)

    print("[INFO] 話者分離開始...")
    diarize_model = whisperx.DiarizationPipeline(use_auth_token=hf_token, device=device)

    diarize_kwargs = {}
    if num_speakers is not None:
        diarize_kwargs["num_speakers"] = num_speakers

    diarize_segments = diarize_model(audio_path, **diarize_kwargs)
    result = whisperx.assign_word_speakers(diarize_segments, result)

    words = []
    for seg in result.get("segments", []):
        speaker = seg.get("speaker", "UNKNOWN")
        for w in seg.get("words", []):
            words.append({
                "text": w.get("word", ""),
                "start": round(w.get("start", 0), 3),
                "end": round(w.get("end", 0), 3),
                "speaker": w.get("speaker", speaker),
                "score": round(w.get("score", 0), 3),
            })

    speakers = sorted(set(w["speaker"] for w in words))

    output = {
        "words": words,
        "speakers": speakers,
        "num_speakers": len(speakers),
        "total_words": len(words),
        "segments": [
            {
                "text": seg.get("text", ""),
                "start": round(seg.get("start", 0), 3),
                "end": round(seg.get("end", 0), 3),
                "speaker": seg.get("speaker", "UNKNOWN"),
            }
            for seg in result.get("segments", [])
        ],
    }

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    result_path = out_path / "stt_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 文字起こし完了: {len(words)}単語, {len(speakers)}話者")
    print(f"[INFO] 話者: {', '.join(speakers)}")
    print(f"[INFO] 保存先: {result_path}")


def main():
    parser = argparse.ArgumentParser(description="WhisperX文字起こし + 話者分離")
    parser.add_argument("--audio", default=None, help="16kHz WAVファイルパス（シングルトラック用）")
    parser.add_argument("--metadata", default=None, help="metadata.jsonパス（マルチトラック用）")
    parser.add_argument("--output", required=True, help="出力ディレクトリ")
    parser.add_argument("--num-speakers", type=int, default=None, help="話者数ヒント")
    args = parser.parse_args()

    if args.metadata:
        with open(args.metadata, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("mode") in ("obs_multitrack", "multitrack", "stereo_split"):
            load_env()
            run_stt_multitrack(args.metadata, args.output)
            return

    if not args.audio:
        print("[ERROR] --audio または --metadata を指定してください", file=sys.stderr)
        sys.exit(1)
    run_stt(args.audio, args.output, args.num_speakers)


if __name__ == "__main__":
    main()
