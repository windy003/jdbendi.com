from flask import Flask, render_template, request, jsonify, session, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import json
import os
import sqlite3
from datetime import timedelta
from dotenv import load_dotenv
import uuid
import time
import re
import threading
import queue

# 加载环境变量
load_dotenv()

app = Flask(__name__)
# 从环境变量读取 secret_key，如果不存在则使用随机生成的（仅用于开发）
app.secret_key = os.getenv('SECRET_KEY', os.urandom(24).hex())
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# CORS 配置 - 允许所有来源（开发环境）
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# ========== SSE 实时推送注册表 ==========
# user_id -> list of Queue，每个 SSE 连接一个队列
_sse_clients: dict[int, list[queue.Queue]] = {}
_sse_lock = threading.Lock()

def _sse_subscribe(user_id: int) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=20)
    with _sse_lock:
        _sse_clients.setdefault(user_id, []).append(q)
    return q

def _sse_unsubscribe(user_id: int, q: queue.Queue):
    with _sse_lock:
        clients = _sse_clients.get(user_id, [])
        try:
            clients.remove(q)
        except ValueError:
            pass

def sse_push(user_id: int, data: dict):
    """向指定用户的所有 SSE 连接推送消息"""
    with _sse_lock:
        clients = list(_sse_clients.get(user_id, []))
    for q in clients:
        try:
            q.put_nowait(data)
        except queue.Full:
            pass  # 队列满则丢弃，不阻塞主线程
# ================================================

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

    # 创建posts表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            contact TEXT NOT NULL,
            images TEXT,
            timestamp INTEGER NOT NULL,
            price TEXT,
            user_id INTEGER,
            location TEXT,
            videos TEXT
        )
    ''')

    # 如果表已存在但没有price字段，则添加price字段
    try:
        cursor.execute("SELECT price FROM posts LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE posts ADD COLUMN price TEXT")

    # 如果表已存在但没有user_id字段，则添加user_id字段
    try:
        cursor.execute("SELECT user_id FROM posts LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE posts ADD COLUMN user_id INTEGER")

    # 如果表已存在但没有location字段，则添加location字段
    try:
        cursor.execute("SELECT location FROM posts LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE posts ADD COLUMN location TEXT")

    # 如果表已存在但没有videos字段，则添加videos字段
    try:
        cursor.execute("SELECT videos FROM posts LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE posts ADD COLUMN videos TEXT")

    # 创建users表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            created_at INTEGER NOT NULL,
            last_login INTEGER
        )
    ''')

    # 创建评论表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            parent_id INTEGER,
            content TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (parent_id) REFERENCES comments(id)
        )
    ''')

    # 创建通知表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            post_id INTEGER,
            comment_id INTEGER,
            from_user_id INTEGER,
            content TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            timestamp INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (from_user_id) REFERENCES users(id)
        )
    ''')

    # 创建索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_username ON users(username)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_role ON users(role)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON users(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id)')

    conn.commit()
    conn.close()

    # 迁移管理员到users表
    migrate_admin_to_users()

def migrate_admin_to_users():
    """将环境变量中的管理员迁移到users表"""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        return

    conn = get_db()
    cursor = conn.cursor()

    # 检查管理员是否已存在
    cursor.execute('SELECT id FROM users WHERE username = ?', (ADMIN_USERNAME,))
    if cursor.fetchone():
        conn.close()
        return  # 已存在，无需重复创建

    # 创建管理员账户
    cursor.execute('''
        INSERT INTO users (username, password_hash, role, status, created_at)
        VALUES (?, ?, 'admin', 'active', ?)
    ''', (ADMIN_USERNAME, ADMIN_PASSWORD_HASH, int(time.time() * 1000)))

    conn.commit()
    conn.close()
    print(f"管理员账户 '{ADMIN_USERNAME}' 已创建")

def validate_username(username):
    """验证用户名格式"""
    if not username or len(username) < 3 or len(username) > 20:
        return False, "用户名长度应为3-20个字符"
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return False, "用户名只能包含字母、数字和下划线"
    return True, ""

def validate_password(password):
    """验证密码格式"""
    if not password or len(password) < 6 or len(password) > 20:
        return False, "密码长度应为6-20个字符"
    return True, ""

def username_exists(username):
    """检查用户名是否已存在"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def authenticate_user(username, password):
    """验证用户身份"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, password_hash, role, status FROM users WHERE username = ?', (username,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return None, "用户名或密码错误"

    if user['status'] == 'disabled':
        conn.close()
        return None, "账户已被禁用"

    if not check_password_hash(user['password_hash'], password):
        conn.close()
        return None, "用户名或密码错误"

    # 更新最后登录时间
    cursor.execute('UPDATE users SET last_login = ? WHERE id = ?',
                   (int(time.time() * 1000), user['id']))
    conn.commit()
    conn.close()

    return {
        'id': user['id'],
        'username': username,
        'role': user['role']
    }, None

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 权限装饰器
def login_required(f):
    """需要登录"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': '未登录'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """需要管理员权限"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': '未登录'}), 401
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'message': '权限不足'}), 403
        return f(*args, **kwargs)
    return decorated_function

def post_owner_or_admin(f):
    """信息所有者或管理员"""
    @wraps(f)
    def decorated_function(post_id, *args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': '未登录'}), 401

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({'success': False, 'message': '信息不存在'}), 404

        post_user_id = row['user_id']
        is_owner = post_user_id == session.get('user_id')
        is_admin = session.get('role') == 'admin'

        if not (is_owner or is_admin):
            return jsonify({'success': False, 'message': '无权操作'}), 403

        return f(post_id, *args, **kwargs)
    return decorated_function

# 路由：前台页面
@app.route('/')
def index():
    return render_template('index.html', admin_contact=ADMIN_CONTACT)

# 路由：帖子详情页
@app.route('/post/<int:post_id>')
def post_detail(post_id):
    return render_template('post_detail.html', post_id=post_id, admin_contact=ADMIN_CONTACT)

# 路由：用户注册页面
@app.route('/register')
def register_page():
    return render_template('register.html')

# 路由：用户登录页面
@app.route('/login')
def login_page():
    return render_template('login.html')

# 路由：用户中心页面
@app.route('/user_center')
def user_center():
    return render_template('user_center.html')

# 路由：后台登录页面
@app.route('/admin')
def admin():
    return render_template('admin.html')

# 路由：管理员用户管理页面
@app.route('/admin/users')
def admin_users():
    return render_template('admin_users.html')

# API：获取所有信息
@app.route('/api/posts', methods=['GET'])
def get_posts():
    conn = get_db()
    cursor = conn.cursor()
    category = request.args.get('category', '全部')

    if category != '全部':
        cursor.execute('''
            SELECT p.*, u.username as author,
                (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) as comment_count
            FROM posts p LEFT JOIN users u ON p.user_id = u.id
            WHERE p.category = ? ORDER BY p.timestamp DESC
        ''', (category,))
    else:
        cursor.execute('''
            SELECT p.*, u.username as author,
                (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) as comment_count
            FROM posts p LEFT JOIN users u ON p.user_id = u.id
            ORDER BY p.timestamp DESC
        ''')

    posts = []
    for row in cursor.fetchall():
        post = {
            'id': row['id'],
            'category': row['category'],
            'title': row['title'],
            'content': row['content'],
            'contact': row['contact'],
            'images': json.loads(row['images']) if row['images'] else [],
            'timestamp': row['timestamp'],
            'price': row['price'] if 'price' in row.keys() else None,
            'location': row['location'] if 'location' in row.keys() else None,
            'author': row['author'] if 'author' in row.keys() else None,
            'user_id': row['user_id'],
            'comment_count': row['comment_count'] if 'comment_count' in row.keys() else 0
        }
        posts.append(post)

    conn.close()
    return jsonify({'success': True, 'data': posts})

# API：获取单条信息详情
@app.route('/api/posts/<int:post_id>', methods=['GET'])
def get_post(post_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.*, u.username as author
        FROM posts p LEFT JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'message': '信息不存在'}), 404
    post = {
        'id': row['id'],
        'category': row['category'],
        'title': row['title'],
        'content': row['content'],
        'contact': row['contact'],
        'images': json.loads(row['images']) if row['images'] else [],
        'timestamp': row['timestamp'],
        'price': row['price'] if 'price' in row.keys() else None,
        'location': row['location'] if 'location' in row.keys() else None,
        'author': row['author'] if 'author' in row.keys() else None,
        'user_id': row['user_id']
    }
    return jsonify({'success': True, 'data': post})

# API：用户注册
@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    # 验证用户名
    valid, msg = validate_username(username)
    if not valid:
        return jsonify({'success': False, 'message': msg}), 400

    # 验证密码
    valid, msg = validate_password(password)
    if not valid:
        return jsonify({'success': False, 'message': msg}), 400

    # 检查用户名是否已存在
    if username_exists(username):
        return jsonify({'success': False, 'message': '用户名已存在'}), 400

    # 创建用户
    password_hash = generate_password_hash(password)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (username, password_hash, role, status, created_at)
        VALUES (?, ?, 'user', 'active', ?)
    ''', (username, password_hash, int(time.time() * 1000)))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': '注册成功'})

# API：用户登录
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'success': False, 'message': '用户名或密码不能为空'}), 400

    # 先尝试从users表验证
    user, error = authenticate_user(username, password)
    if user:
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        session.permanent = True
        return jsonify({
            'success': True,
            'message': '登录成功',
            'user': {
                'id': user['id'],
                'username': user['username'],
                'role': user['role']
            }
        })

    return jsonify({'success': False, 'message': error}), 401

# API：检查登录状态
@app.route('/api/check_login', methods=['GET'])
def check_login():
    if 'user_id' in session:
        return jsonify({
            'success': True,
            'logged_in': True,
            'user': {
                'id': session.get('user_id'),
                'username': session.get('username'),
                'role': session.get('role')
            }
        })
    return jsonify({'success': True, 'logged_in': False})

# API：退出登录
@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True, 'message': '已退出登录'})

# API：发布信息（需要登录）
@app.route('/api/posts', methods=['POST'])
@login_required
def create_post():
    data = request.get_json()

    conn = get_db()
    cursor = conn.cursor()

    images_json = json.dumps(data.get('images', []))

    cursor.execute('''
        INSERT INTO posts (category, title, content, contact, images, timestamp, price, user_id, location)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('category'),
        data.get('title'),
        data.get('content'),
        data.get('contact'),
        images_json,
        data.get('timestamp'),
        data.get('price'),
        session.get('user_id'),
        data.get('location')
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
        'timestamp': data.get('timestamp'),
        'price': data.get('price'),
        'user_id': session.get('user_id'),
        'location': data.get('location')
    }

    return jsonify({'success': True, 'message': '发布成功', 'data': new_post})

# API：更新信息（需要登录且是所有者或管理员）
@app.route('/api/posts/<int:post_id>', methods=['PUT'])
@post_owner_or_admin
def update_post(post_id):
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
        SET category = ?, title = ?, content = ?, contact = ?, images = ?, price = ?, location = ?
        WHERE id = ?
    ''', (
        data.get('category'),
        data.get('title'),
        data.get('content'),
        data.get('contact'),
        images_json,
        data.get('price'),
        data.get('location'),
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
        'timestamp': data.get('timestamp'),
        'price': data.get('price'),
        'location': data.get('location')
    }

    return jsonify({'success': True, 'message': '更新成功', 'data': updated_post})

# API：获取我的信息
@app.route('/api/my_posts', methods=['GET'])
@login_required
def get_my_posts():
    user_id = session.get('user_id')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM posts WHERE user_id = ? ORDER BY timestamp DESC', (user_id,))

    posts = []
    for row in cursor.fetchall():
        post = {
            'id': row['id'],
            'category': row['category'],
            'title': row['title'],
            'content': row['content'],
            'contact': row['contact'],
            'images': json.loads(row['images']) if row['images'] else [],
            'timestamp': row['timestamp'],
            'price': row['price'] if 'price' in row.keys() else None,
            'user_id': row['user_id'],
            'location': row['location'] if 'location' in row.keys() else None
        }
        posts.append(post)

    conn.close()
    return jsonify({'success': True, 'data': posts})

# API：删除信息（需要登录且是所有者或管理员）
@app.route('/api/posts/<int:post_id>', methods=['DELETE'])
@post_owner_or_admin
def delete_post(post_id):

    conn = get_db()
    cursor = conn.cursor()

    # 先获取图片列表，删除图片文件
    cursor.execute('SELECT images FROM posts WHERE id = ?', (post_id,))
    row = cursor.fetchone()
    if row and row['images']:
        for image in json.loads(row['images']):
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
@login_required
def upload_image():
    try:
        print("=== 图片上传请求 ===")
        print(f"登录用户: {session.get('username')}")

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

        # 直接从原始文件名提取扩展名（避免 secure_filename 剥掉中文后丢失扩展名）
        if '.' not in original_filename:
            print("错误: 文件没有扩展名")
            return jsonify({'success': False, 'message': '文件必须有扩展名'}), 400

        ext = original_filename.rsplit('.', 1)[1].lower()
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

# 路由：访问上传的文件
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# 路由：微信收款码
@app.route('/wechat_pay.png')
def wechat_pay():
    return send_from_directory('.', '微信收款码.png')

# API：获取用户列表（仅管理员）
@app.route('/api/admin/users', methods=['GET'])
@admin_required
def get_users():
    conn = get_db()
    cursor = conn.cursor()

    # 获取用户列表及其发布数量
    cursor.execute('''
        SELECT
            u.id,
            u.username,
            u.role,
            u.status,
            u.created_at,
            u.last_login,
            COUNT(p.id) as posts_count
        FROM users u
        LEFT JOIN posts p ON u.id = p.user_id
        GROUP BY u.id
        ORDER BY u.created_at DESC
    ''')

    users = []
    for row in cursor.fetchall():
        user = {
            'id': row['id'],
            'username': row['username'],
            'role': row['role'],
            'status': row['status'],
            'created_at': row['created_at'],
            'last_login': row['last_login'],
            'posts_count': row['posts_count']
        }
        users.append(user)

    conn.close()
    return jsonify({'success': True, 'data': users})

# API：禁用/启用用户（仅管理员）
@app.route('/api/admin/users/<int:user_id>/status', methods=['PUT'])
@admin_required
def update_user_status(user_id):
    data = request.get_json()
    new_status = data.get('status')

    if new_status not in ['active', 'disabled']:
        return jsonify({'success': False, 'message': '状态值无效'}), 400

    # 不能禁用自己
    if user_id == session.get('user_id'):
        return jsonify({'success': False, 'message': '不能禁用自己的账户'}), 400

    conn = get_db()
    cursor = conn.cursor()

    # 检查用户是否存在
    cursor.execute('SELECT id FROM users WHERE id = ?', (user_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'success': False, 'message': '用户不存在'}), 404

    # 更新状态
    cursor.execute('UPDATE users SET status = ? WHERE id = ?', (new_status, user_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': '用户状态已更新'})

# API：获取统计数据（仅管理员）
@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def get_stats():
    conn = get_db()
    cursor = conn.cursor()

    # 总用户数
    cursor.execute('SELECT COUNT(*) as count FROM users')
    total_users = cursor.fetchone()['count']

    # 活跃用户数
    cursor.execute('SELECT COUNT(*) as count FROM users WHERE status = "active"')
    active_users = cursor.fetchone()['count']

    # 禁用用户数
    cursor.execute('SELECT COUNT(*) as count FROM users WHERE status = "disabled"')
    disabled_users = cursor.fetchone()['count']

    # 总信息数
    cursor.execute('SELECT COUNT(*) as count FROM posts')
    total_posts = cursor.fetchone()['count']

    # 今日发布数（最近24小时）
    today_timestamp = int(time.time() * 1000) - 24 * 60 * 60 * 1000
    cursor.execute('SELECT COUNT(*) as count FROM posts WHERE timestamp > ?', (today_timestamp,))
    posts_today = cursor.fetchone()['count']

    conn.close()

    stats = {
        'total_users': total_users,
        'active_users': active_users,
        'disabled_users': disabled_users,
        'total_posts': total_posts,
        'posts_today': posts_today
    }

    return jsonify({'success': True, 'data': stats})

# ========== 评论 API ==========

def parse_mentions(content):
    """解析评论内容中的 @用户名"""
    return re.findall(r'@([a-zA-Z0-9_]+)', content)

NOTIF_MAX_PER_USER = 200      # 每个用户最多保留通知数
NOTIF_READ_EXPIRE_DAYS = 30   # 已读通知保留天数

def cleanup_notifications(cursor, user_id):
    """清理旧通知：删除已读且超过30天的，以及超出上限的最旧记录"""
    expire_ts = int(time.time() * 1000) - NOTIF_READ_EXPIRE_DAYS * 86400 * 1000
    # 删除已读且过期的
    cursor.execute(
        'DELETE FROM notifications WHERE user_id = ? AND is_read = 1 AND timestamp < ?',
        (user_id, expire_ts)
    )
    # 保留最新的 NOTIF_MAX_PER_USER 条，删除超出部分
    cursor.execute('''
        DELETE FROM notifications WHERE user_id = ? AND id NOT IN (
            SELECT id FROM notifications WHERE user_id = ?
            ORDER BY timestamp DESC LIMIT ?
        )
    ''', (user_id, user_id, NOTIF_MAX_PER_USER))

def send_notification(cursor, user_id, ntype, post_id, comment_id, from_user_id, content):
    """发送通知（不重复通知自己），并记录待推送目标"""
    if user_id == from_user_id:
        return
    cursor.execute('''
        INSERT INTO notifications (user_id, type, post_id, comment_id, from_user_id, content, is_read, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
    ''', (user_id, ntype, post_id, comment_id, from_user_id, content, int(time.time() * 1000)))
    cleanup_notifications(cursor, user_id)
    # 记录到线程本地，等 commit 后再推送（避免推送时数据还未落库）
    if not hasattr(_pending_sse, 'targets'):
        _pending_sse.targets = []
    _pending_sse.targets.append(user_id)

# 线程本地存储，用于收集本次请求需要推送的用户
_pending_sse = threading.local()

# API：获取某帖子的评论
@app.route('/api/posts/<int:post_id>/comments', methods=['GET'])
def get_comments(post_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT c.id, c.post_id, c.user_id, c.parent_id, c.content, c.timestamp,
               u.username
        FROM comments c
        LEFT JOIN users u ON c.user_id = u.id
        WHERE c.post_id = ?
        ORDER BY c.timestamp ASC
    ''', (post_id,))
    rows = cursor.fetchall()
    conn.close()
    comments = [{
        'id': r['id'],
        'post_id': r['post_id'],
        'user_id': r['user_id'],
        'parent_id': r['parent_id'],
        'content': r['content'],
        'timestamp': r['timestamp'],
        'username': r['username']
    } for r in rows]
    return jsonify({'success': True, 'data': comments})

# API：发表评论（需要登录）
@app.route('/api/posts/<int:post_id>/comments', methods=['POST'])
@login_required
def create_comment(post_id):
    data = request.get_json()
    content = data.get('content', '').strip()
    parent_id = data.get('parent_id')  # 回复哪条评论

    if not content:
        return jsonify({'success': False, 'message': '评论内容不能为空'}), 400
    if len(content) > 500:
        return jsonify({'success': False, 'message': '评论内容不能超过500字'}), 400

    conn = get_db()
    cursor = conn.cursor()

    # 验证帖子存在
    cursor.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
    post_row = cursor.fetchone()
    if not post_row:
        conn.close()
        return jsonify({'success': False, 'message': '帖子不存在'}), 404

    now = int(time.time() * 1000)
    from_uid = session.get('user_id')

    cursor.execute('''
        INSERT INTO comments (post_id, user_id, parent_id, content, timestamp)
        VALUES (?, ?, ?, ?, ?)
    ''', (post_id, from_uid, parent_id, content, now))
    comment_id = cursor.lastrowid

    # 通知帖子作者（有人评论了我的帖子）
    if post_row['user_id']:
        send_notification(cursor, post_row['user_id'], 'comment', post_id, comment_id, from_uid, content)

    # 如果是回复，通知被回复的评论作者
    if parent_id:
        cursor.execute('SELECT user_id FROM comments WHERE id = ?', (parent_id,))
        parent_row = cursor.fetchone()
        if parent_row and parent_row['user_id'] != post_row['user_id']:
            send_notification(cursor, parent_row['user_id'], 'reply', post_id, comment_id, from_uid, content)

    # 解析 @用户名，发通知
    mentions = parse_mentions(content)
    notified = set()
    if post_row['user_id']:
        notified.add(post_row['user_id'])
    if parent_id:
        cursor.execute('SELECT user_id FROM comments WHERE id = ?', (parent_id,))
        pr = cursor.fetchone()
        if pr:
            notified.add(pr['user_id'])
    for mention in mentions:
        cursor.execute('SELECT id FROM users WHERE username = ?', (mention,))
        mu = cursor.fetchone()
        if mu and mu['id'] not in notified:
            send_notification(cursor, mu['id'], 'mention', post_id, comment_id, from_uid, content)
            notified.add(mu['id'])

    conn.commit()

    # commit 后推送 SSE（数据已落库，前端此时拉取通知一定能拿到）
    targets = getattr(_pending_sse, 'targets', [])
    for uid in targets:
        sse_push(uid, {'type': 'new_notification'})
    _pending_sse.targets = []

    # 返回完整评论对象
    cursor.execute('''
        SELECT c.id, c.post_id, c.user_id, c.parent_id, c.content, c.timestamp, u.username
        FROM comments c LEFT JOIN users u ON c.user_id = u.id
        WHERE c.id = ?
    ''', (comment_id,))
    row = cursor.fetchone()
    conn.close()

    return jsonify({'success': True, 'data': {
        'id': row['id'], 'post_id': row['post_id'], 'user_id': row['user_id'],
        'parent_id': row['parent_id'], 'content': row['content'],
        'timestamp': row['timestamp'], 'username': row['username']
    }})

# API：删除评论（本人或管理员）
@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@login_required
def delete_comment(comment_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM comments WHERE id = ?', (comment_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': '评论不存在'}), 404
    if row['user_id'] != session.get('user_id') and session.get('role') != 'admin':
        conn.close()
        return jsonify({'success': False, 'message': '无权删除'}), 403
    cursor.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '评论已删除'})

# ========== 通知 API ==========

# API：获取我的通知
@app.route('/api/notifications', methods=['GET'])
@login_required
def get_notifications():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT n.id, n.type, n.post_id, n.comment_id, n.content, n.is_read, n.timestamp,
               u.username as from_username,
               p.title as post_title
        FROM notifications n
        LEFT JOIN users u ON n.from_user_id = u.id
        LEFT JOIN posts p ON n.post_id = p.id
        WHERE n.user_id = ?
        ORDER BY n.timestamp DESC
        LIMIT 50
    ''', (session.get('user_id'),))
    rows = cursor.fetchall()
    conn.close()
    notifications = [{
        'id': r['id'], 'type': r['type'], 'post_id': r['post_id'],
        'comment_id': r['comment_id'], 'content': r['content'],
        'is_read': r['is_read'], 'timestamp': r['timestamp'],
        'from_username': r['from_username'], 'post_title': r['post_title']
    } for r in rows]
    return jsonify({'success': True, 'data': notifications})

# API：获取未读通知数量
@app.route('/api/notifications/unread_count', methods=['GET'])
@login_required
def get_unread_count():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) as cnt FROM notifications WHERE user_id = ? AND is_read = 0',
                   (session.get('user_id'),))
    count = cursor.fetchone()['cnt']
    conn.close()
    return jsonify({'success': True, 'count': count})

# API：标记所有通知为已读
@app.route('/api/notifications/read_all', methods=['POST'])
@login_required
def read_all_notifications():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE notifications SET is_read = 1 WHERE user_id = ?', (session.get('user_id'),))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# API：SSE 实时通知流（需要登录）
@app.route('/api/notifications/stream')
def notification_stream():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登录'}), 401

    user_id = session.get('user_id')
    q = _sse_subscribe(user_id)

    @stream_with_context
    def generate():
        # 首次连接立即发一次心跳，让浏览器确认连接成功
        yield 'data: {"type":"connected"}\n\n'
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f'data: {json.dumps(data, ensure_ascii=False)}\n\n'
                except queue.Empty:
                    # 25 秒无消息发心跳，防止代理/浏览器断开连接
                    yield 'data: {"type":"heartbeat"}\n\n'
        except GeneratorExit:
            pass
        finally:
            _sse_unsubscribe(user_id, q)

    resp = Response(generate(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'   # 关闭 Nginx 缓冲
    resp.headers['Connection'] = 'keep-alive'
    return resp

# API：搜索用户名（用于 @ 提示）
@app.route('/api/users/search', methods=['GET'])
def search_users():
    q = request.args.get('q', '').strip()
    if not q or len(q) < 1:
        return jsonify({'success': True, 'data': []})
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT username FROM users
        WHERE username LIKE ? AND status = 'active'
        LIMIT 8
    ''', (q + '%',))
    users = [r['username'] for r in cursor.fetchall()]
    conn.close()
    return jsonify({'success': True, 'data': users})


if __name__ == '__main__':
    # 创建必要的目录
    os.makedirs('templates', exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # 初始化数据库
    init_db()

    app.run(debug=True, host='0.0.0.0', port=5002, threaded=True)
