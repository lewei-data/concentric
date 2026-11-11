package com.boray.voicemcp;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;

/**
 * OPC 数字员工档案（server v2.5）。
 *
 * 列表分两块：「在册员工」= 除老板外的所有员工；页面最后单独一个「老板」专栏，
 * 只有老板本人（OPC-000）。老板不混在员工列表里，避免把「汇报对象」和「老板自己」混为一谈。
 */
public class EmployeeListActivity extends Activity {

    private LinearLayout llEmployees;
    private LinearLayout llBoss;
    private View llBossSection;
    private TextView tvCount, tvSyncState, tvBossNo;
    private Button btnSync;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_employee_list);
        WindowInsetsHelper.apply(this);
        llEmployees = findViewById(R.id.llEmployees);
        llBoss = findViewById(R.id.llBoss);
        llBossSection = findViewById(R.id.llBossSection);
        tvBossNo = findViewById(R.id.tvBossNo);
        tvCount = findViewById(R.id.tvCount);
        tvSyncState = findViewById(R.id.tvSyncState);
        btnSync = findViewById(R.id.btnSync);
        View back = findViewById(R.id.btnBack);
        if (back != null) back.setOnClickListener(v -> finish());

        btnSync.setOnClickListener(v -> refreshFromServer());
    }

    @Override
    protected void onResume() {
        super.onResume();
        render();
        EmployeeStore.syncIfStale(this);   // 缓存过期时后台静默同步
    }

    private void refreshFromServer() {
        tvSyncState.setText("正在从服务端 profile 同步…");
        btnSync.setEnabled(false);
        EmployeeStore.syncFromServer(this, McpConfig.load(this), (count, error) -> {
            btnSync.setEnabled(true);
            if (error.isEmpty()) {
                tvSyncState.setText("已从服务端同步 " + count + " 位员工 · " + EmployeeStore.syncStatus(this));
                toast("档案已更新：" + count + " 位");
                render();
            } else {
                tvSyncState.setText("同步失败：" + error + "（继续使用本机缓存档案）");
                toast("同步失败：" + error);
            }
        });
    }

    private void render() {
        List<Employee> all = EmployeeStore.load(this);
        Employee current = EmployeeStore.current(this, all);
        llEmployees.removeAllViews();
        if (llBoss != null) llBoss.removeAllViews();

        Employee boss = null;
        int staff = 0;
        for (Employee e : all) {
            if (isBoss(e)) {
                if (boss == null) boss = e;
                continue;
            }
            staff++;
            llEmployees.addView(row(e, current, llEmployees));
        }

        tvCount.setText(staff + " 位");
        tvSyncState.setText(EmployeeStore.syncStatus(this)
                + " · 素材地址：服务端 profile / MCP 同源");

        // 老板专栏：只有老板本人，放在页面最后
        if (boss != null && llBoss != null && llBossSection != null) {
            llBossSection.setVisibility(View.VISIBLE);
            if (tvBossNo != null) {
                tvBossNo.setText((boss.no.isEmpty() ? "OPC-000" : boss.no)
                        + (boss.dept.isEmpty() ? "" : " · " + boss.dept));
            }
            llBoss.addView(row(boss, current, llBoss));
        } else if (llBossSection != null) {
            llBossSection.setVisibility(View.GONE);
        }
    }

    /** 老板判定统一走 Employee.isBoss()（详情页也用它） */
    private static boolean isBoss(Employee e) {
        return e != null && e.isBoss();
    }

    /** 渲染一行员工卡片（在册员工与老板专栏共用同一套样式） */
    private View row(Employee e, Employee current, LinearLayout parent) {
        LayoutInflater inflater = LayoutInflater.from(this);
        View row = inflater.inflate(R.layout.item_employee, parent, false);
        TextView name = row.findViewById(R.id.tvName);
        TextView noDept = row.findViewById(R.id.tvNoDept);
        TextView title = row.findViewById(R.id.tvTitle);
        TextView meta = row.findViewById(R.id.tvMeta);
        TextView skills = row.findViewById(R.id.tvSkills);
        TextView badge = row.findViewById(R.id.tvCurrent);
        ImageView photo = row.findViewById(R.id.ivPhoto);
        DigitalHumanView avatar = row.findViewById(R.id.dhvAvatar);

        name.setText(e.name);
        String role = e.title.isEmpty() ? e.role : e.title;
        noDept.setText(e.no + " · " + e.dept);
        title.setText(role + (e.project.isEmpty() ? "" : " · " + e.project));
        meta.setText(e.metaLine() + " · " + e.assetLine());
        skills.setText(join(e.skills));
        boolean isCurrent = current != null && current.id.equals(e.id);
        badge.setVisibility(isCurrent ? View.VISIBLE : View.GONE);

        final DigitalHumanView builtIn = avatar;
        final ImageView photoView = photo;
        builtIn.setVisibility(View.VISIBLE);
        photoView.setVisibility(View.GONE);
        builtIn.setEmployee(e);
        builtIn.setStaticPose(true);
        EmployeeStore.applyPhoto(this, e, photoView,
                () -> {
                    builtIn.setVisibility(View.GONE);
                    photoView.setVisibility(View.VISIBLE);
                },
                () -> {
                    builtIn.setVisibility(View.VISIBLE);
                    photoView.setVisibility(View.GONE);
                });

        final String id = e.id;
        View.OnClickListener open = v -> {
            Intent i = new Intent(this, EmployeeDetailActivity.class);
            i.putExtra(EmployeeDetailActivity.EXTRA_ID, id);
            startActivity(i);
        };
        row.findViewById(R.id.cardEmployee).setOnClickListener(open);
        row.setOnClickListener(open);
        return row;
    }

    private static String join(List<String> items) {
        if (items == null || items.isEmpty()) return "—";
        StringBuilder b = new StringBuilder("擅长：");
        for (int i = 0; i < items.size() && i < 4; i++) {
            if (i > 0) b.append(" · ");
            b.append(items.get(i));
        }
        return b.toString();
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }
}
