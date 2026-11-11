package com.boray.voicemcp;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;

/**
 * 直接录 16kHz / 16bit / 单声道 PCM 并封装成标准 WAV。
 * 服务端 ASR 明确支持 wav，且不依赖手机上的编码器，兼容性最好。
 */
public final class WavRecorder {

    private static final int SAMPLE_RATE = 16000;
    /** 最长 60 秒，约 1.9MB，base64 后约 2.6MB */
    private static final long MAX_PCM_BYTES = 16000L * 2 * 60;

    private AudioRecord audioRecord;
    private RandomAccessFile raf;
    private Thread reader;
    private volatile boolean running;
    private long pcmBytes;
    private File outFile;

    public void start(File target) throws IOException {
        outFile = target;
        pcmBytes = 0;
        int minBuffer = AudioRecord.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        int bufferSize = Math.max(minBuffer, SAMPLE_RATE / 5 * 2);

        audioRecord = new AudioRecord(MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufferSize);
        if (audioRecord.getState() != AudioRecord.STATE_INITIALIZED) {
            audioRecord.release();
            audioRecord = null;
            throw new IOException("麦克风初始化失败，请检查录音权限或被其它应用占用");
        }

        raf = new RandomAccessFile(target, "rw");
        raf.setLength(0);
        writeHeader(raf, 0);

        running = true;
        audioRecord.startRecording();
        final int size = bufferSize;
        reader = new Thread(() -> {
            byte[] buf = new byte[size];
            while (running) {
                int n = audioRecord.read(buf, 0, buf.length);
                if (n > 0) {
                    try {
                        raf.write(buf, 0, n);
                        pcmBytes += n;
                    } catch (IOException e) {
                        break;
                    }
                    if (pcmBytes >= MAX_PCM_BYTES) {
                        running = false;
                        break;
                    }
                } else if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_BAD_VALUE) {
                    break;
                }
            }
        }, "wav-recorder");
        reader.start();
    }

    /** 停止录音并回写 WAV 长度字段；返回可直接 base64 上传的文件 */
    public File stop() {
        running = false;
        if (audioRecord != null) {
            try {
                if (audioRecord.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    audioRecord.stop();
                }
            } catch (IllegalStateException ignored) {
            }
        }
        if (reader != null) {
            try { reader.join(2000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
        if (audioRecord != null) {
            audioRecord.release();
            audioRecord = null;
        }
        if (raf != null) {
            try {
                writeHeader(raf, pcmBytes);
                raf.close();
            } catch (IOException ignored) {
            }
            raf = null;
        }
        return (outFile != null && pcmBytes > 0) ? outFile : null;
    }

    public long recordedBytes() {
        return pcmBytes;
    }

    private static void writeHeader(RandomAccessFile f, long dataLength) throws IOException {
        f.seek(0);
        f.writeBytes("RIFF");
        writeInt(f, (int) (36 + dataLength));
        f.writeBytes("WAVE");
        f.writeBytes("fmt ");
        writeInt(f, 16);                 // fmt chunk 长度
        writeShort(f, 1);                // PCM
        writeShort(f, 1);                // 单声道
        writeInt(f, SAMPLE_RATE);        // 采样率
        writeInt(f, SAMPLE_RATE * 2);    // 字节率 = rate * channels * bits / 8
        writeShort(f, 2);                // block align
        writeShort(f, 16);               // 位深
        f.writeBytes("data");
        writeInt(f, (int) dataLength);
    }

    private static void writeInt(RandomAccessFile f, int v) throws IOException {
        f.write(v & 0xFF);
        f.write((v >>> 8) & 0xFF);
        f.write((v >>> 16) & 0xFF);
        f.write((v >>> 24) & 0xFF);
    }

    private static void writeShort(RandomAccessFile f, int v) throws IOException {
        f.write(v & 0xFF);
        f.write((v >>> 8) & 0xFF);
    }
}
