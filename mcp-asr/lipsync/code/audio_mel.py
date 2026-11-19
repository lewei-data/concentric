"""Wav2Lip 音频前端：复刻官方 audio.py（librosa mel），保证与权重训练时一致。"""
import librosa
import librosa.filters
import numpy as np
from scipy import signal

SR = 16000
N_FFT = 800
HOP = 200
WIN = 800
N_MELS = 80
FMIN = 55
FMAX = 7600
PREEMPHASIS = 0.97
REF_LEVEL_DB = 20
MIN_LEVEL_DB = -100
MAX_ABS_VALUE = 4.0

_mel_basis = None


def load_wav(path, sr=SR):
    return librosa.core.load(path, sr=sr)[0]


def _build_mel_basis():
    return librosa.filters.mel(sr=SR, n_fft=N_FFT, n_mels=N_MELS, fmin=FMIN, fmax=FMAX)


def _linear_to_mel(spec):
    global _mel_basis
    if _mel_basis is None:
        _mel_basis = _build_mel_basis()
    return np.dot(_mel_basis, spec)


def _amp_to_db(x):
    min_level = np.exp(MIN_LEVEL_DB / 20 * np.log(10))
    return 20 * np.log10(np.maximum(min_level, x))


def _normalize(S):
    return np.clip((2 * MAX_ABS_VALUE) * ((S - MIN_LEVEL_DB) / (-MIN_LEVEL_DB)) - MAX_ABS_VALUE,
                   -MAX_ABS_VALUE, MAX_ABS_VALUE)


def melspectrogram(wav):
    y = signal.lfilter([1, -PREEMPHASIS], [1], wav)
    D = librosa.stft(y=y, n_fft=N_FFT, hop_length=HOP, win_length=WIN)
    S = _amp_to_db(_linear_to_mel(np.abs(D))) - REF_LEVEL_DB
    return _normalize(S)
