import time
import statistics
from datetime import datetime, timedelta
import pytest
from unittest.mock import MagicMock

from app.services.sheet_service import sheet_service
from app.services.session_manager import session_manager
from app.services.llm_service import llm_service, split_sentences
from app.utils.phonetics import parse_spoken_hindi_numbers

def test_performance_and_latency_profiling(protect_production_excel, monkeypatch):
    """
    Sub-Phase E Requirement:
    Measure and report empirical latencies for:
    - Number recognition & phonetic parsing.
    - Sentence splitting for streaming TTS.
    - Excel slot validation latency.
    - Atomic Excel appointment persistence latency (with lock, backup, and CallSid check).
    - Webhook turn processing round-trip latency.
    """
    temp_excel = protect_production_excel
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. Phonetic & Spoken Number Parsing Latency (100 iterations)
    test_phrases = [
        "7233 4566 81",
        "mera number double seven double zero one two three four five six hai",
        "call me on 9829 012 345 please",
        "aath aath do char char chhe chhe do do nau"
    ]
    stt_times = []
    for _ in range(25):
        for phrase in test_phrases:
            t0 = time.perf_counter()
            res = parse_spoken_hindi_numbers(phrase)
            t1 = time.perf_counter()
            stt_times.append((t1 - t0) * 1000)  # in ms

    # 2. Sentence Splitting Latency for Micro-TTS (100 iterations)
    tts_text = (
        "Hello, welcome to Dr. Sharma Care Clinic. I have noted your appointment for tomorrow at 11 AM. "
        "Shall I confirm this booking? Please let me know."
    )
    tts_split_times = []
    for _ in range(100):
        t0 = time.perf_counter()
        sentences, _ = split_sentences(tts_text)
        t1 = time.perf_counter()
        tts_split_times.append((t1 - t0) * 1000)

    # 3. Excel Slot Validation Latency (50 iterations)
    validation_times = []
    for _ in range(50):
        t0 = time.perf_counter()
        valid, _ = sheet_service.validate_slot(tomorrow_str, "11:00 AM")
        t1 = time.perf_counter()
        validation_times.append((t1 - t0) * 1000)

    # 4. Excel Atomic Persistence Latency (with lock, backup, and CallSid scan) (15 iterations)
    save_times = []
    for idx in range(15):
        slot = sheet_service.DEFAULT_SLOTS[idx % len(sheet_service.DEFAULT_SLOTS)]
        t0 = time.perf_counter()
        sheet_service.save_appointment(
            patient_name=f"Perf Patient {idx}",
            phone_number=f"98290{idx:05d}",
            date_str=tomorrow_str,
            time_slot=slot,
            symptoms="Benchmark checkup",
            call_sid=f"CA_PERF_{idx}"
        )
        t1 = time.perf_counter()
        save_times.append((t1 - t0) * 1000)

    # 5. Full Conversational Turn Processing Round-Trip Latency (simulated LLM turn)
    mock_gemini = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Yes, I am listening. Please go ahead."}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini)

    turn_times = []
    for idx in range(20):
        t0 = time.perf_counter()
        llm_service.process_turn(f"CA_TURN_PERF_{idx}", "Hello, I want to book an appointment")
        t1 = time.perf_counter()
        turn_times.append((t1 - t0) * 1000)

    metrics = {
        "stt_parse_ms_median": statistics.median(stt_times),
        "stt_parse_ms_p95": sorted(stt_times)[int(len(stt_times) * 0.95)],
        "tts_split_ms_median": statistics.median(tts_split_times),
        "tts_split_ms_p95": sorted(tts_split_times)[int(len(tts_split_times) * 0.95)],
        "excel_validate_ms_median": statistics.median(validation_times),
        "excel_validate_ms_p95": sorted(validation_times)[int(len(validation_times) * 0.95)],
        "excel_save_ms_median": statistics.median(save_times),
        "excel_save_ms_p95": sorted(save_times)[int(len(save_times) * 0.95)],
        "turn_roundtrip_ms_median": statistics.median(turn_times),
        "turn_roundtrip_ms_p95": sorted(turn_times)[int(len(turn_times) * 0.95)],
    }

    print("\n" + "=" * 60)
    print("EMPIRICAL LATENCY BENCHMARK RESULTS (Sub-Phase E)")
    print("=" * 60)
    print(f"• STT Spoken Number Parsing:   Median={metrics['stt_parse_ms_median']:.3f} ms | p95={metrics['stt_parse_ms_p95']:.3f} ms")
    print(f"• TTS Sentence Splitting:      Median={metrics['tts_split_ms_median']:.3f} ms | p95={metrics['tts_split_ms_p95']:.3f} ms")
    print(f"• Excel Slot Validation:       Median={metrics['excel_validate_ms_median']:.2f} ms | p95={metrics['excel_validate_ms_p95']:.2f} ms")
    print(f"• Excel Atomic Save & Lock:    Median={metrics['excel_save_ms_median']:.2f} ms | p95={metrics['excel_save_ms_p95']:.2f} ms")
    print(f"• Turn Round-Trip (Turn logic): Median={metrics['turn_roundtrip_ms_median']:.2f} ms | p95={metrics['turn_roundtrip_ms_p95']:.2f} ms")
    print("=" * 60)

    # Assertions on critical performance budgets
    assert metrics["stt_parse_ms_median"] < 5.0, "Phonetic parsing exceeds 5ms budget!"
    assert metrics["tts_split_ms_median"] < 1.0, "TTS chunk splitting exceeds 1ms budget!"
    assert metrics["excel_validate_ms_median"] < 50.0, "Excel validation exceeds 50ms budget!"
    assert metrics["excel_save_ms_median"] < 150.0, "Excel write exceeds 150ms budget!"
    assert metrics["turn_roundtrip_ms_median"] < 100.0, "In-memory turn routing exceeds 100ms budget!"
