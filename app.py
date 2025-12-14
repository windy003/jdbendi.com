from flask import Flask, render_template, request, jsonify, session, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import json
import os
import sqlite3
from datetime import timedelta
from dotenv import load_dotenv
import uuid

# 加载环境变量
load_dotenv()

app = Flask(__name__)
# 从环境变量读取 secret_key，如果不存在则使用随机生成的（仅用于开发）
app.secret_key = os.getenv('SECRET_KEY', os.urandom(24).hex())
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# CORS 配置 - 允许所有来源（开发环境）
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# ========== 配置区域（从环境变量读取，保护敏感信息） ==========
# 账号密码（使用哈希加密存储）
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
admin_password = os.getenv('ADMIN_PASSWORD')  # 默认仅用于开发
ADMIN_PASSWORD_HASH = generate_password_hash(admin_password)

# 站长联系方式
ADMIN_CONTACT = os.getenv('ADMIN_CONTACT', "周秋良:手机:15868404601,微信同号")

# 图片上传配置
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_IMAGES = 9  # 每条信息最多上传9张图片
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 限制16MB
# ========================================================

# 数据库文件路径
DATABASE = 'posts.db'

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            contact TEXT NOT NULL,
            images TEXT,
            timestamp INTEGER NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 路由：前台页面
@app.route('/')
def index():
    return render_template('index.html', admin_contact=ADMIN_CONTACT)

# 路由：后台登录页面
@app.route('/admin')
def admin():
    return render_template('admin.html')

# API：获取所有信息
@app.route('/api/posts', methods=['GET'])
def get_posts():
    conn = get_db()
    cursor = conn.cursor()
    category = request.args.get('category', '全部')

    if category != '全部':
        cursor.execute('SELECT * FROM posts WHERE category = ? ORDER BY timestamp DESC', (category,))
    else:
        cursor.execute('SELECT * FROM posts ORDER BY timestamp DESC')

    posts = []
    for row in cursor.fetchall():
        post = {
            'id': row['id'],
            'category': row['category'],
            'title': row['title'],
            'content': row['content'],
            'contact': row['contact'],
            'images': json.loads(row['images']) if row['images'] else [],
            'timestamp': row['timestamp']
        }
        posts.append(post)

    conn.close()
    return jsonify({'success': True, 'data': posts})

# API：站长登录
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')

    if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
        session['admin_logged_in'] = True
        session.permanent = True
        return jsonify({'success': True, 'message': '登录成功'})
    else:
        return jsonify({'success': False, 'message': '账号或密码错误'})

# API：检查登录状态
@app.route('/api/check_login', methods=['GET'])
def check_login():
    is_logged_in = session.get('admin_logged_in', False)
    return jsonify({'success': True, 'logged_in': is_logged_in})

# API：退出登录
@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('admin_logged_in', None)
    return jsonify({'success': True, 'message': '已退出登录'})

# API：发布信息（需要登录）
@app.route('/api/posts', methods=['POST'])
def create_post():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': '未登录'}), 401

    data = request.get_json()

    conn = get_db()
    cursor = conn.cursor()

    images_json = json.dumps(data.get('images', []))

    cursor.execute('''
        INSERT INTO posts (category, title, content, contact, images, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        data.get('category'),
        data.get('title'),
        data.get('content'),
        data.get('contact'),
        images_json,
        data.get('timestamp')
    ))

    conn.commit()
    post_id = cursor.lastrowid
    conn.close()

    new_post = {
        'id': post_id,
        'category': data.get('category'),
        'title': data.get('title'),
        'content': data.get('content'),
        'contact': data.get('contact'),
        'images': data.get('images', []),
        'timestamp': data.get('timestamp')
    }

    return jsonify({'success': True, 'message': '发布成功', 'data': new_post})

# API：更新信息（需要登录）
@app.route('/api/posts/<int:post_id>', methods=['PUT'])
def update_post(post_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': '未登录'}), 401

    data = request.get_json()

    conn = get_db()
    cursor = conn.cursor()

    # 获取原有的图片列表
    cursor.execute('SELECT images FROM posts WHERE id = ?', (post_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return jsonify({'success': False, 'message': '信息不存在'}), 404

    old_images = json.loads(row['images']) if row['images'] else []
    new_images = data.get('images', [])

    # 删除不再使用的图片文件
    for image in old_images:
        if image not in new_images:
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], image)
            if os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except:
                    pass

    images_json = json.dumps(new_images)

    # 更新数据库记录
    cursor.execute('''
        UPDATE posts
        SET category = ?, title = ?, content = ?, contact = ?, images = ?
        WHERE id = ?
    ''', (
        data.get('category'),
        data.get('title'),
        data.get('content'),
        data.get('contact'),
        images_json,
        post_id
    ))

    conn.commit()
    conn.close()

    updated_post = {
        'id': post_id,
        'category': data.get('category'),
        'title': data.get('title'),
        'content': data.get('content'),
        'contact': data.get('contact'),
        'images': new_images,
        'timestamp': data.get('timestamp')
    }

    return jsonify({'success': True, 'message': '更新成功', 'data': updated_post})

# API：删除信息（需要登录）
@app.route('/api/posts/<int:post_id>', methods=['DELETE'])
def delete_post(post_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': '未登录'}), 401

    conn = get_db()
    cursor = conn.cursor()

    # 先获取图片列表，删除图片文件
    cursor.execute('SELECT images FROM posts WHERE id = ?', (post_id,))
    row = cursor.fetchone()
    if row and row['images']:
        images = json.loads(row['images'])
        for image in images:
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], image)
            if os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except:
                    pass

    # 删除数据库记录
    cursor.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': '删除成功'})

# API：上传图片（需要登录）
@app.route('/api/upload', methods=['POST'])
def upload_image():
    try:
        print("=== 图片上传请求 ===")
        print(f"登录状态: {session.get('admin_logged_in')}")

        if not session.get('admin_logged_in'):
            print("错误: 未登录")
            return jsonify({'success': False, 'message': '未登录'}), 401

        # 检查请求内容长度
        print(f"Content-Length: {request.content_length}")
        print(f"Content-Type: {request.content_type}")

        # 访问 request.files 可能会阻塞，如果文件很大
        print("开始读取上传文件...")
        print(f"请求文件: {list(request.files.keys())}")
        print("文件读取完成")

        if 'image' not in request.files:
            print("错误: 未找到 'image' 字段")
            return jsonify({'success': False, 'message': '未选择文件'}), 400

        file = request.files['image']
        original_filename = file.filename
        print(f"原始文件名: {original_filename}")

        if not original_filename or original_filename == '':
            print("错误: 文件名为空")
            return jsonify({'success': False, 'message': '未选择文件'}), 400

        # 使用 secure_filename 处理文件名
        safe_filename = secure_filename(original_filename)
        print(f"安全文件名: {safe_filename}")

        if not safe_filename:
            print("错误: 文件名处理后为空")
            return jsonify({'success': False, 'message': '文件名不合法'}), 400

        # 检查文件扩展名
        if '.' not in safe_filename:
            print("错误: 文件没有扩展名")
            return jsonify({'success': False, 'message': '文件必须有扩展名'}), 400

        ext = safe_filename.rsplit('.', 1)[1].lower()
        print(f"文件扩展名: {ext}")

        if ext not in ALLOWED_EXTENSIONS:
            allowed = ', '.join(ALLOWED_EXTENSIONS)
            print(f"错误: 不支持的文件格式 '{ext}'，允许的格式: {allowed}")
            return jsonify({'success': False, 'message': f'不支持的文件格式，仅支持: {allowed}'}), 400

        # 生成唯一文件名
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        print(f"保存路径: {filepath}")

        # 确保上传目录存在
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

        # 保存文件
        file.save(filepath)

        # 验证文件是否保存成功
        if os.path.exists(filepath):
            file_size = os.path.getsize(filepath)
            print(f"上传成功: {filename}, 大小: {file_size} bytes")
            return jsonify({'success': True, 'filename': filename})
        else:
            print("错误: 文件保存后不存在")
            return jsonify({'success': False, 'message': '文件保存失败'}), 500

    except Exception as e:
        print(f"上传异常: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'上传失败: {str(e)}'}), 500

# 路由：访问上传的图片
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    # 创建必要的目录
    os.makedirs('templates', exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # 初始化数据库
    init_db()

    # 运行应用
    print("=" * 50)
    print("服务器启动成功！")
    print("前台页面: http://127.0.0.1:5000/")
    print("后台管理: http://127.0.0.1:5000/admin")
    print("默认账号: admin")
    print("默认密码: 123456")
    print("=" * 50)
    app.run(debug=True, host='0.0.0.0', port=5002)
