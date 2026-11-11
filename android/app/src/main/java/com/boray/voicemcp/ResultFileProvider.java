package com.boray.voicemcp;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;
import java.io.IOException;

/**
 * 极简文件提供者：把 files/results/ 下的结果文件以 content:// 暴露给外部应用。
 * 不依赖 AndroidX，避免引入额外 Maven 依赖。
 */
public final class ResultFileProvider extends ContentProvider {

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        Context ctx = getContext();
        if (ctx == null) throw new FileNotFoundException("no context");
        String path = uri.getPath();
        boolean isHistory = path != null && path.contains("/" + ResultStore.HISTORY_PREFIX);
        File base = isHistory ? ResultStore.historyDir(ctx) : ResultStore.resultDir(ctx);
        File root;
        try {
            root = base.getCanonicalFile();
        } catch (IOException e) {
            throw new FileNotFoundException(e.getMessage());
        }
        String segment = uri.getLastPathSegment();
        if (segment == null) throw new FileNotFoundException("empty filename");
        // 只取文件名，杜绝 ../ 目录穿越
        File target = new File(root, new File(segment).getName());
        try {
            String canonical = target.getCanonicalPath();
            if (!canonical.startsWith(root.getCanonicalPath() + File.separator)) {
                throw new FileNotFoundException("invalid path");
            }
        } catch (IOException e) {
            throw new FileNotFoundException(e.getMessage());
        }
        if (!target.isFile()) throw new FileNotFoundException(target.getName());
        return ParcelFileDescriptor.open(target, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public String getType(Uri uri) {
        return ResultStore.mimeOf(uri.getLastPathSegment());
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
