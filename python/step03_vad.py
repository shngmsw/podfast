"""Step 03: Silero VAD 無音区間検出"""

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import torch
from silero_vad import load_silero_vad, get_speech_timestamps


def load_wav_as_tensor(audio_path: str, sampling_rate: int = 16000) -> torch.Tensor:
    """WAVファイルをtorchテンソルとして読み込み（torchcodec不要）"""
    import wave
    with wave.open(audio_path, "rb") as wf:
        assert wf.getsampwidth() == 2, "16-bit WAV expected"
        n_channels = wf.getnchannels()
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    # リサンプリング（必要な場合）
    if sr != sampling_rate:
        import torchaudio.functional as F
        t = torch.from_numpy(samples).unsqueeze(0)
        t = F.resample(t, sr, sampling_rate)
        return t.squeeze(0)
    return torch.from_numpy(samples)


def vad_single(audio_path: str, model, threshold, min_silence_ms, speech_pad_ms):
    """1ファイルのVAD実行"""
    wav = load_wav_as_tensor(audio_path, sampling_rate=16000)
    total_duration = len(wav) / 16000

    speech_timestamps = get_speech_timestamps(
        wav, model,
        threshold=threshold,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
        min_speech_duration_ms=250,
        sampling_rate=16000,
    )

    speech_segments = [
        {
            "start": round(ts["start"] / 16000, 3),
            "end": round(ts["end"] / 16000, 3),
        }
        for ts in speech_timestamps
    ]

    silence_segments = []
    prev_end = 0.0
    for seg in speech_segments:
        if seg["start"] - prev_end > 0.1:
            silence_segments.append({
                "start": round(prev_end, 3),
                "end": round(seg["start"], 3),
                "duration": round(seg["start"] - prev_end, 3),
            })
        prev_end = seg["end"]

    if total_duration - prev_end > 0.1:
        silence_segments.append({
            "start": round(prev_end, 3),
            "end": round(total_duration, 3),
            "duration": round(total_duration - prev_end, 3),
        })

    total_silence = sum(s["duration"] for s in silence_segments)
    silence_ratio = total_silence / total_duration if total_duration > 0 else 0

    return {
        "speech_segments": speech_segments,
        "silence_segments": silence_segments,
        "stats": {
            "total_duration": round(total_duration, 2),
            "speech_duration": round(total_duration - total_silence, 2),
            "silence_duration": round(total_silence, 2),
            "silence_ratio": round(silence_ratio, 4),
            "num_speech_segments": len(speech_segments),
            "num_silence_segments": len(silence_segments),
        }
    }


def run_vad(audio_path: str, stt_path: str, output_dir: str, metadata_path: str | None = None):
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    threshold = float(os.getenv("VAD_THRESHOLD", "0.5"))
    min_silence_ms = int(os.getenv("VAD_MIN_SILENCE_MS", "500"))
    speech_pad_ms = int(os.getenv("VAD_SPEECH_PAD_MS", "200"))

    torch.set_num_threads(1)
    model = load_silero_vad()

    print(f"[INFO] VADパラメータ: threshold={threshold}, min_silence={min_silence_ms}ms, pad={speech_pad_ms}ms")

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # マルチトラック判定
    metadata = None
    if metadata_path:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

    if metadata and metadata.get("mode") in ("obs_multitrack", "multitrack", "stereo_split"):
        print(f"[INFO] マルチトラックVAD: {len(metadata['tracks'])}トラック")
        all_vad = {}
        # 全トラック統合用
        all_silence = []
        total_duration = 0

        for track in metadata["tracks"]:
            track_path = Path(track["path"]).resolve()
            if not track_path.exists():
                track_path = (Path(metadata_path).parent / track["path"]).resolve()
            speaker = track["speaker"]
            print(f"[INFO] VAD実行: {speaker}")
            result = vad_single(str(track_path), model, threshold, min_silence_ms, speech_pad_ms)
            all_vad[speaker] = result
            total_duration = max(total_duration, result["stats"]["total_duration"])

            # トラック別に保存
            speaker_path = out_path / f"vad_{speaker}.json"
            with open(speaker_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"[INFO] {speaker}: 音声{result['stats']['num_speech_segments']}区間, 無音{result['stats']['num_silence_segments']}区間, 無音率{result['stats']['silence_ratio']:.1%}")

        # 全トラック共通の無音区間（全員が無音の区間）を算出
        # 全トラックのspeech_segmentsを統合
        all_speech = []
        for speaker_result in all_vad.values():
            all_speech.extend(speaker_result["speech_segments"])
        all_speech.sort(key=lambda s: s["start"])

        # マージ（重なるspeech区間を結合）
        merged_speech = []
        for seg in all_speech:
            if merged_speech and seg["start"] <= merged_speech[-1]["end"]:
                merged_speech[-1]["end"] = max(merged_speech[-1]["end"], seg["end"])
            else:
                merged_speech.append(dict(seg))

        # 統合無音区間
        silence_segments = []
        prev_end = 0.0
        for seg in merged_speech:
            if seg["start"] - prev_end > 0.1:
                silence_segments.append({
                    "start": round(prev_end, 3),
                    "end": round(seg["start"], 3),
                    "duration": round(seg["start"] - prev_end, 3),
                })
            prev_end = seg["end"]
        if total_duration - prev_end > 0.1:
            silence_segments.append({
                "start": round(prev_end, 3),
                "end": round(total_duration, 3),
                "duration": round(total_duration - prev_end, 3),
            })

        total_silence = sum(s["duration"] for s in silence_segments)
        silence_ratio = total_silence / total_duration if total_duration > 0 else 0

        combined = {
            "speech_segments": merged_speech,
            "silence_segments": silence_segments,
            "per_speaker": {speaker: r for speaker, r in all_vad.items()},
            "stats": {
                "total_duration": round(total_duration, 2),
                "speech_duration": round(total_duration - total_silence, 2),
                "silence_duration": round(total_silence, 2),
                "silence_ratio": round(silence_ratio, 4),
                "num_speech_segments": len(merged_speech),
                "num_silence_segments": len(silence_segments),
            }
        }

        result_path = out_path / "vad_result.json"
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(combined, f, ensure_ascii=False, indent=2)

        print(f"[INFO] 統合VAD完了: 音声{len(merged_speech)}区間, 全員無音{len(silence_segments)}区間")
        print(f"[INFO] 統合無音率: {silence_ratio:.1%} ({total_silence:.1f}秒 / {total_duration:.1f}秒)")
    else:
        # シングルトラック
        result = vad_single(audio_path, model, threshold, min_silence_ms, speech_pad_ms)
        result_path = out_path / "vad_result.json"
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        stats = result["stats"]
        print(f"[INFO] VAD完了: 音声{stats['num_speech_segments']}区間, 無音{stats['num_silence_segments']}区間")
        print(f"[INFO] 無音率: {stats['silence_ratio']:.1%} ({stats['silence_duration']:.1f}秒 / {stats['total_duration']:.1f}秒)")

    print(f"[INFO] 保存先: {result_path}")


def main():
    parser = argparse.ArgumentParser(description="Silero VAD 無音検出")
    parser.add_argument("--audio", default=None)
    parser.add_argument("--stt", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", default=None, help="metadata.jsonパス（マルチトラック用）")
    args = parser.parse_args()
    run_vad(args.audio, args.stt, args.output, args.metadata)


if __name__ == "__main__":
    main()
