import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)
DB_FILE = 'lottery.db'
ROUND_SETTINGS_FILE = 'round_settings.json'

# ==========================================
# ⚙️ 轮次时间持久化配置 (保证重启不丢失清空状态)
# ==========================================
def load_round_times():
    """从本地文件加载每个节点的重置时间"""
    if os.path.exists(ROUND_SETTINGS_FILE):
        try:
            with open(ROUND_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_round_times(data):
    """将重置时间保存到本地文件"""
    with open(ROUND_SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# 全局变量，存储各节点的本轮起始时间: {"node_123": "2023-10-25 15:30:00"}
round_start_times = load_round_times()


# ==========================================
# 🗄️ 数据库初始化与连接
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # 节点表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nodes (
            device_id TEXT PRIMARY KEY,
            nickname TEXT,
            password TEXT,
            status TEXT DEFAULT '未运行',
            last_active DATETIME
        )
    ''')
    # 日志表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            nickname TEXT,
            quantity INTEGER,
            log_time DATETIME
        )
    ''')
    # 历史修正表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history_manual (
            device_id TEXT,
            date TEXT,
            manual_sum INTEGER,
            manual_users INTEGER,
            PRIMARY KEY (device_id, date)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ==========================================
# 🌐 页面路由
# ==========================================
@app.route('/')
def index():
    return render_template('dashboard.html')

# ==========================================
# 🔌 API 路由
# ==========================================

@app.route('/api/nodes', methods=['GET'])
def get_nodes():
    """获取所有节点列表及在线状态"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT device_id, nickname, password, last_active, status FROM nodes ORDER BY last_active DESC")
    rows = cursor.fetchall()
    conn.close()

    nodes = []
    now = datetime.now()
    for r in rows:
        # 判断是否在线 (假设 5 分钟内有活动视为在线)
        is_online = False
        if r['last_active']:
            last_active_time = datetime.strptime(r['last_active'], '%Y-%m-%d %H:%M:%S')
            if (now - last_active_time).total_seconds() < 300:
                is_online = True

        nodes.append({
            "device_id": r['device_id'],
            "nickname": r['nickname'] or r['device_id'][:8],
            "has_password": bool(r['password']),
            "is_online": is_online
        })
    return jsonify({"nodes": nodes})


@app.route('/api/node/delete', methods=['POST'])
def delete_node():
    """删除节点及其所有数据"""
    device_id = request.json.get('device_id')
    if not device_id:
        return jsonify({"error": "Missing device_id"}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM nodes WHERE device_id = ?", (device_id,))
    cursor.execute("DELETE FROM logs WHERE device_id = ?", (device_id,))
    cursor.execute("DELETE FROM history_manual WHERE device_id = ?", (device_id,))
    conn.commit()
    conn.close()
    
    # 清理内存中的重置时间记录
    if device_id in round_start_times:
        del round_start_times[device_id]
        save_round_times(round_start_times)

    return jsonify({"status": "success"})


@app.route('/api/reset_round', methods=['POST'])
def reset_round():
    """手动开启新一轮，刷新 Today 数据显示"""
    device_id = request.json.get('device_id')
    if not device_id:
        return jsonify({"status": "error", "message": "Missing device_id"}), 400
        
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    round_start_times[device_id] = now_str
    save_round_times(round_start_times)
    
    return jsonify({"status": "success", "round_start_time": now_str})


@app.route('/api/stats', methods=['GET'])
def get_stats():
    device_id = request.args.get('node_id')
    password = request.args.get('password', '')
    
    if not device_id:
        return jsonify({"error": "Missing node_id"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. 验证节点和密码
    cursor.execute("SELECT password, status, last_active FROM nodes WHERE device_id = ?", (device_id,))
    node = cursor.fetchone()
    if not node:
        conn.close()
        return jsonify({"error": "Node not found"}), 404
        
    if node['password'] and node['password'] != password:
        conn.close()
        return jsonify({"error": "Forbidden"}), 403

    process_status = node['status']
    if node['last_active']:
        last_active_time = datetime.strptime(node['last_active'], '%Y-%m-%d %H:%M:%S')
        if (datetime.now() - last_active_time).total_seconds() >= 300:
            process_status = "离线"

    # ==========================================
    # 🔥 核心升级：跨天自由轮次 + 48小时保底逻辑
    # ==========================================
    now = datetime.now()
    start_of_today = now.strftime('%Y-%m-%d 00:00:00')
    
    # 设定 48 小时的极限边界
    limit_48h = (now - timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')

    if device_id in round_start_times:
        node_round_start = round_start_times[device_id]
        # 如果手动重置过，取【重置时间】和【48小时前】中较晚的一个
        # 效果：哪怕跨越了午夜 12 点，数据依然会保留；但最长不会显示超过 48 小时的数据。
        effective_start_time = max(limit_48h, node_round_start)
    else:
        # 如果这个节点从来没点过重置，默认只看今天的
        effective_start_time = max(limit_48h, start_of_today)

    try:
        # --- 获取 本轮 实时数据 ---
        cursor.execute('''
            SELECT nickname, log_time, quantity 
            FROM logs 
            WHERE device_id = ? AND log_time >= ?
            ORDER BY log_time DESC
        ''', (device_id, effective_start_time))
        details = [dict(row) for row in cursor.fetchall()]
        
        cursor.execute('''
            SELECT nickname, COUNT(*) as win_times, SUM(quantity) as win_sum 
            FROM logs 
            WHERE device_id = ? AND log_time >= ?
            GROUP BY nickname 
            ORDER BY win_sum DESC
        ''', (device_id, effective_start_time))
        ranks = [dict(row) for row in cursor.fetchall()]
        
        total_users = len(ranks)
        total_wins = sum(row['quantity'] for row in details)

        # --- 获取 历史数据 (完全不受影响，依然按天结算) ---
        cursor.execute('''
            SELECT 
                substr(log_time, 1, 10) as date, 
                COUNT(DISTINCT nickname) as user_count, 
                SUM(quantity) as daily_sum 
            FROM logs 
            WHERE device_id = ? 
            GROUP BY date 
            ORDER BY date DESC
            LIMIT 30
        ''', (device_id,))
        raw_history = cursor.fetchall()
        
        # 获取人工修正数据
        cursor.execute('SELECT date, manual_sum, manual_users FROM history_manual WHERE device_id = ?', (device_id,))
        manual_records = {row['date']: row for row in cursor.fetchall()}

        history_data = []
        today_date = now.strftime('%Y-%m-%d')
        for row in raw_history:
            date_str = row['date']
            # 今天的数据不放入“历史记录”列表中
            if date_str == today_date:
                continue
                
            manual = manual_records.get(date_str)
            if manual:
                history_data.append({
                    "date": date_str,
                    "user_count": manual['manual_users'],
                    "daily_sum": manual['manual_sum'],
                    "is_manual": True
                })
            else:
                history_data.append({
                    "date": date_str,
                    "user_count": row['user_count'],
                    "daily_sum": row['daily_sum'],
                    "is_manual": False
                })

        # 🔥 格式化给前端显示的时间范围：截取 MM-DD HH:MM (例如 "10-25 23:00")
        display_time = effective_start_time[5:16]

        return jsonify({
            "process_status": process_status,
            "total_users": total_users,
            "total_wins": total_wins,
            "date_range": f"{display_time} - 至今",  # 传给前端显示
            "rank_list": ranks,
            "details": details,
            "history_data": history_data
        })

    except Exception as e:
        print(f"Stats Error: {e}")
        return jsonify({"error": "Database query failed"}), 500
    finally:
        conn.close()


@app.route('/api/history_logs', methods=['GET'])
def get_history_logs():
    """获取指定历史日期的详细记录"""
    device_id = request.args.get('node_id')
    date_str = request.args.get('date')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT log_time, nickname, quantity 
        FROM logs 
        WHERE device_id = ? AND substr(log_time, 1, 10) = ?
        ORDER BY log_time DESC
    ''', (device_id, date_str))
    logs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({"logs": logs})


@app.route('/api/update_history', methods=['POST'])
def update_history():
    """手动修正历史数据"""
    data = request.json
    device_id = data.get('device_id')
    date_str = data.get('date')
    manual_sum = data.get('manual_sum')
    manual_users = data.get('manual_users')
    
    if not all([device_id, date_str]):
        return jsonify({"error": "Missing parameters"}), 400
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO history_manual (device_id, date, manual_sum, manual_users)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device_id, date) DO UPDATE SET
        manual_sum = excluded.manual_sum,
        manual_users = excluded.manual_users
    ''', (device_id, date_str, manual_sum, manual_users))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success"})


# ==========================================
# 🚀 启动服务
# ==========================================
if __name__ == '__main__':
    # 确保保存 JSON 的目录可写
    app.run(host='0.0.0.0', port=5000, debug=True)