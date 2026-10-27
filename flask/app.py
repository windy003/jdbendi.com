from flask import Flask, render_template, request, jsonify, session, send_from_directory, redirect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
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
import oss2
import mimetypes
import subprocess
import tempfile
import shutil
import smtplib
import ipaddress
import requests
import secrets
from alibabacloud_dypnsapi20170525.client import Client as DypnsapiClient
from alibabacloud_dypnsapi20170525 import models as dypnsapi_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util.models import RuntimeOptions
from email.mime.text import MIMEText
from email.header import Header

# 注册 .apk 的 MIME 类型，避免静态文件被当成 application/octet-stream 下载成 .bin
mimetypes.add_type('application/vnd.android.package-archive', '.apk')
# 注册 .svg 的 MIME 类型：Windows 注册表里该项常缺失或错误（如 image/svg），
# 导致浏览器 <img> 标签拒绝渲染 SVG（显示加载失败占位图），这里强制指定为正确的 image/svg+xml
mimetypes.add_type('image/svg+xml', '.svg')

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

# ========== 配置区域（从环境变量读取，保护敏感信息） ==========
# 账号密码（使用哈希加密存储）
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
admin_password = os.getenv('ADMIN_PASSWORD')  # 默认仅用于开发
ADMIN_PASSWORD_HASH = generate_password_hash(admin_password)

# 站长联系方式
ADMIN_CONTACT = os.getenv('ADMIN_CONTACT', "周秋良:手机:15868404601,微信同号")

# 邮件推送配置（新帖子/新评论/删除时发送邮件通知站长）
SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '465'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
SMTP_USE_SSL = os.getenv('SMTP_USE_SSL', 'true').lower() != 'false'
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '')

# 媒体上传配置
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'webm', 'mkv'}
ALLOWED_ALL_MEDIA = ALLOWED_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS
MAX_IMAGES = 9  # 每条信息最多上传9张图片
MAX_VIDEOS = 3  # 每条信息最多上传3个视频

# 头像配置：用户自定义头像统一存放在 OSS 的 Avatar/ 目录下；未自定义时使用默认头像
AVATAR_OSS_FOLDER = 'Avatar'
DEFAULT_AVATAR_URL = 'https://jdbendi.oss-cn-hangzhou.aliyuncs.com/Avatar/default_avatar.svg'
MAX_BIO_LENGTH = 60  # 一句话介绍最多60个字符
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 限制500MB（支持视频）

# 阿里云 OSS 配置
OSS_ACCESS_KEY_ID = os.getenv('OSS_ACCESS_KEY_ID', '')
OSS_ACCESS_KEY_SECRET = os.getenv('OSS_ACCESS_KEY_SECRET', '')
OSS_BUCKET_NAME = os.getenv('OSS_BUCKET_NAME', '')
OSS_ENDPOINT = os.getenv('OSS_ENDPOINT', '')  # 例如: oss-cn-hangzhou.aliyuncs.com
OSS_PUBLIC_URL = os.getenv('OSS_PUBLIC_URL', '')  # 例如: https://jdbendi.oss-cn-hangzhou.aliyuncs.com
# 阿里云短信服务（未配置时短信登录接口返回清晰的配置提示）
SMS_ACCESS_KEY_ID = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_ID', '')
SMS_ACCESS_KEY_SECRET = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_SECRET', '')
SMS_SIGN_NAME = os.getenv('ALIYUN_PNVS_SIGN_NAME', '')
SMS_TEMPLATE_CODE = os.getenv('ALIYUN_PNVS_TEMPLATE_CODE', '')
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
            contact TEXT,
            images TEXT,
            timestamp INTEGER NOT NULL,
            price TEXT,
            user_id INTEGER,
            location TEXT,
            videos TEXT,
            updated_at INTEGER,
            is_public INTEGER NOT NULL DEFAULT 1
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

    # 如果表已存在但没有updated_at字段，则添加updated_at字段（记录修改时间，NULL表示未修改过）
    try:
        cursor.execute("SELECT updated_at FROM posts LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE posts ADD COLUMN updated_at INTEGER")

    # 如果表已存在但没有is_public字段，则添加is_public字段（1=所有人可见，0=仅自己可见）
    try:
        cursor.execute("SELECT is_public FROM posts LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE posts ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1")

    # 创建users表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            phone TEXT UNIQUE,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            created_at INTEGER NOT NULL,
            last_login INTEGER
        )
    ''')

    # Existing installations gain phone support without changing username/password accounts.
    try:
        cursor.execute("SELECT phone FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sms_login_codes (
            phone TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL DEFAULT '',
            out_id TEXT,
            expires_at INTEGER NOT NULL,
            last_sent_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        )
    ''')
    try:
        cursor.execute("SELECT out_id FROM sms_login_codes LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE sms_login_codes ADD COLUMN out_id TEXT")
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone)')

    # 如果表已存在但没有avatar_url字段，则添加avatar_url字段（用户自定义头像，NULL表示使用默认头像）
    try:
        cursor.execute("SELECT avatar_url FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")

    # 如果表已存在但没有bio字段，则添加bio字段（一句话介绍自己，NULL表示未填写）
    try:
        cursor.execute("SELECT bio FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE users ADD COLUMN bio TEXT")

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

    # 创建私信表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            content TEXT,
            content_type TEXT NOT NULL DEFAULT 'text',
            media_url TEXT,
            timestamp INTEGER NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (from_user_id) REFERENCES users(id),
            FOREIGN KEY (to_user_id) REFERENCES users(id)
        )
    ''')

    # 创建访问记录表（用于统计网站访问量）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT,
            user_id INTEGER,
            timestamp INTEGER NOT NULL
        )
    ''')

    # 创建索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_visits_timestamp ON visits(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_visits_ip ON visits(ip)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_username ON users(username)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_role ON users(role)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON users(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_users ON messages(from_user_id, to_user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_user_id)')

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

def generate_default_username(cursor):
    """Generate a short, non-phone username for SMS-created accounts."""
    while True:
        username = f'user_{secrets.token_hex(4)}'
        cursor.execute('SELECT 1 FROM users WHERE username = ?', (username,))
        if not cursor.fetchone():
            return username

def authenticate_user(username, password):
    """验证用户身份"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, password_hash, role, status, avatar_url, bio FROM users WHERE username = ?', (username,))
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
        'role': user['role'],
        'avatar_url': user['avatar_url'],
        'bio': user['bio'] or ''
    }, None

def valid_phone(phone):
    return bool(re.fullmatch(r'1[3-9]\d{9}', phone or ''))

def send_aliyun_sms(phone):
    if not all((SMS_ACCESS_KEY_ID, SMS_ACCESS_KEY_SECRET, SMS_SIGN_NAME, SMS_TEMPLATE_CODE)):
        raise RuntimeError('短信认证尚未配置阿里云凭证及号码认证服务提供的签名和模板')
    config = open_api_models.Config(
        access_key_id=SMS_ACCESS_KEY_ID,
        access_key_secret=SMS_ACCESS_KEY_SECRET
    )
    config.endpoint = 'dypnsapi.aliyuncs.com'
    client = DypnsapiClient(config)
    out_id = uuid.uuid4().hex
    request = dypnsapi_models.SendSmsVerifyCodeRequest(
        phone_number=phone,
        sign_name=SMS_SIGN_NAME,
        template_code=SMS_TEMPLATE_CODE,
        template_param=json.dumps({'code': '##code##', 'min': '5'}),
        country_code='86',
        out_id=out_id,
        code_length=6,
        code_type=1,
        valid_time=300,
        interval=60,
        return_verify_code=False
    )
    response = client.send_sms_verify_code_with_options(request, RuntimeOptions())
    body = getattr(response, 'body', None)
    if not body or getattr(body, 'code', None) != 'OK' or getattr(body, 'success', False) is False:
        raise RuntimeError(getattr(body, 'message', None) or '阿里云短信发送失败')
    return out_id

def verify_aliyun_sms(phone, code, out_id):
    if not all((SMS_ACCESS_KEY_ID, SMS_ACCESS_KEY_SECRET)):
        raise RuntimeError('短信认证尚未配置阿里云访问凭证')
    config = open_api_models.Config(
        access_key_id=SMS_ACCESS_KEY_ID,
        access_key_secret=SMS_ACCESS_KEY_SECRET
    )
    config.endpoint = 'dypnsapi.aliyuncs.com'
    client = DypnsapiClient(config)
    request = dypnsapi_models.CheckSmsVerifyCodeRequest(
        phone_number=phone,
        verify_code=code,
        out_id=out_id,
        country_code='86',
        case_auth_policy=1
    )
    response = client.check_sms_verify_code_with_options(request, RuntimeOptions())
    body = getattr(response, 'body', None)
    model = getattr(body, 'model', None)
    return bool(body and getattr(body, 'code', None) == 'OK' and
                model and getattr(model, 'verify_result', None) == 'PASS')

def allowed_file(filename):
    """检查图片文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_avatar_url(avatar_url):
    """返回用户头像URL，未自定义时返回默认头像"""
    return avatar_url if avatar_url else DEFAULT_AVATAR_URL

def allowed_media(filename):
    """检查图片或视频扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_ALL_MEDIA

def get_content_type(ext):
    """根据扩展名获取 MIME 类型"""
    types = {
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
        'mp4': 'video/mp4', 'mov': 'video/quicktime',
        'avi': 'video/x-msvideo', 'webm': 'video/webm', 'mkv': 'video/x-matroska'
    }
    return types.get(ext.lower(), 'application/octet-stream')

# ========== 阿里云 OSS 辅助函数 ==========
def get_oss_bucket():
    """获取阿里云 OSS Bucket 客户端"""
    auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
    return oss2.Bucket(auth, f'https://{OSS_ENDPOINT}', OSS_BUCKET_NAME)

def upload_to_oss(file_data, filename, content_type):
    """上传文件到 OSS，返回公开 URL"""
    bucket = get_oss_bucket()
    bucket.put_object(filename, file_data, headers={'Content-Type': content_type})
    return f"{OSS_PUBLIC_URL}/{filename}"

def delete_from_oss(key):
    """从 OSS 删除指定 key 的文件"""
    try:
        bucket = get_oss_bucket()
        bucket.delete_object(key)
    except Exception as e:
        print(f"OSS 删除失败: {e}")

def delete_media(url):
    """从 OSS 删除媒体文件（通过 URL 提取 key，保留目录前缀如 Avatar/xxx.png）"""
    # Only delete objects addressed by our own public OSS URL. Falling back to
    # the last URL segment can accidentally delete an unrelated object.
    if not url or not OSS_PUBLIC_URL or not url.startswith(OSS_PUBLIC_URL.rstrip('/') + '/'):
        return
    key = url[len(OSS_PUBLIC_URL.rstrip('/')) + 1:].split('?', 1)[0]
    if key:
        delete_from_oss(key)


def get_referenced_media(cursor):
    """Return media URLs that are still referenced by database records."""
    referenced = set()
    cursor.execute('SELECT avatar_url FROM users WHERE avatar_url IS NOT NULL')
    referenced.update(row['avatar_url'] for row in cursor.fetchall())
    cursor.execute('SELECT images, videos FROM posts')
    for post in cursor.fetchall():
        for field in ('images', 'videos'):
            try:
                values = json.loads(post[field]) if post[field] else []
            except (TypeError, json.JSONDecodeError):
                values = []
            if isinstance(values, list):
                referenced.update(url for url in values if isinstance(url, str))
    cursor.execute('SELECT media_url FROM messages WHERE media_url IS NOT NULL')
    referenced.update(row['media_url'] for row in cursor.fetchall())
    return referenced

def setup_oss_cors():
    """启动时自动为 OSS 存储桶配置 CORS，允许网站直传"""
    try:
        origins = [
            'https://jdbendi.com',
            'https://www.jdbendi.com',
            'http://localhost:5002',
            'http://127.0.0.1:5002',
        ]
        bucket = get_oss_bucket()
        rule = oss2.models.CorsRule(
            allowed_origins=origins,
            allowed_methods=['GET', 'PUT', 'HEAD'],
            allowed_headers=['*'],
            expose_headers=['ETag'],
            max_age_seconds=86400
        )
        bucket.put_bucket_cors(oss2.models.BucketCors(rules=[rule]))
        print('OSS CORS 配置成功')
    except Exception as e:
        print(f'OSS CORS 配置失败（不影响运行）: {e}')
# ============================================

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

# ========== 访问量统计 ==========
BOT_UA_PATTERN = re.compile(r'bot|spider|crawl|curl|wget|python-requests|headless', re.I)

# 本机反向代理（Caddy）可能用 IPv4 或 IPv6 回环地址连过来
LOOPBACK_PROXIES = {'127.0.0.1', '::1'}

def get_client_ip():
    """获取真实访客 IP：网站经 Caddy 反向代理，request.remote_addr 拿到的是本机代理地址。

    链路是 Caddy(本机) -> waitress -> Flask，waitress 默认会把 X-Forwarded-* 头全部丢掉
    （clear_untrusted_proxy_headers=True），所以以前怎么读 XFF 都读不到，只能退回 127.0.0.1。
    现在启动时用 trusted_proxy='127.0.0.1' 声明信任本机代理，waitress 会把 Caddy 追加在
    X-Forwarded-For 末尾的真实客户端 IP 解析出来，直接重写成 request.remote_addr，
    所以正常情况读 remote_addr 就是真实 IP。

    不要再读 CF-Connecting-IP：站点已不走 Cloudflare，该头没人清理，客户端可随意伪造。
    """
    ip = (request.remote_addr or '').strip()
    # 兜底：请求仍来自本机代理时（代理地址不是 127.0.0.1、trusted_proxy 没命中），
    # 自己去读 X-Forwarded-For —— 只取最后一段，因为 Caddy 把真实 IP 追加在最右侧，
    # 最左边是客户端可以随意伪造的，取它会往访客表里灌假 IP，污染独立访客统计。
    if ip in LOOPBACK_PROXIES:
        xff = request.headers.get('X-Forwarded-For', '')
        if xff:
            ip = xff.rsplit(',', 1)[-1].strip()
    # 校验合法性，非法内容直接丢弃，避免垃圾数据进入统计
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return ''
    return ip

# ========== IP 归属地（ip2region 离线库） ==========
# 用官方 Python 绑定 + data/ 下的 xdb 数据文件在本地查询归属地，
# 仅用于把 24 小时独立 IP 拆分成 杭州市 / 浙江省 / 全国 / 全球 四个口径
# （全国只统计中国境内 IP，全球统计所有独立 IP，包含境外及无法识别归属地的 IP）。
# 后台“查看归属地”按钮仍然走 /api/admin/ip-geo（ip-api.com），两条路径互不影响。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
IP2REGION_DATA_DIR = os.path.join(APP_DIR, 'data')
IP2REGION_DB_FILES = {
    4: os.path.join(IP2REGION_DATA_DIR, 'ip2region_v4.xdb'),
    6: os.path.join(IP2REGION_DATA_DIR, 'ip2region_v6.xdb'),
}

_ip2region_searchers = {}
_ip2region_loaded = False
_ip2region_lock = threading.Lock()

# 24 小时独立 IP 的四个统计口径（顺序仅用于后端校验，前端按需排列）
VISITOR_SCOPES = ('global', 'nationwide', 'zhejiang', 'hangzhou')

def _get_ip2region_searchers():
    """懒加载 ip2region xdb（整库读入内存，查询无磁盘 IO，多线程安全）。"""
    global _ip2region_loaded
    if _ip2region_loaded:
        return _ip2region_searchers
    with _ip2region_lock:
        if not _ip2region_loaded:
            try:
                import ip2region.util as ip2r_util
                from ip2region.searcher import new_with_buffer
            except ImportError as exc:
                app.logger.warning('ip2region 绑定未找到，按地区统计将不可用: %s', exc)
                _ip2region_loaded = True
                return _ip2region_searchers

            for version, db_path in IP2REGION_DB_FILES.items():
                if not os.path.isfile(db_path):
                    app.logger.warning('ip2region 数据文件不存在: %s', db_path)
                    continue
                try:
                    content = ip2r_util.load_content_from_file(db_path)
                    ip_version = ip2r_util.IPv4 if version == 4 else ip2r_util.IPv6
                    _ip2region_searchers[version] = new_with_buffer(ip_version, content)
                except Exception as exc:
                    app.logger.warning('加载 ip2region 数据文件失败 %s: %s', db_path, exc)
            _ip2region_loaded = True
    return _ip2region_searchers

def ip2region_available():
    """ip2region 是否至少加载成功一个数据文件。"""
    return bool(_get_ip2region_searchers())

def lookup_ip_region(ip):
    """查询 IP 归属地，返回 dict；内网/非法/IPv6 库缺失/查不到时返回 None。

    xdb 记录格式：
      新版 v4/v6：国家|省份|城市|ISP|国家代码
      旧版 v2   ：国家|区域|省份|城市|ISP（中国的“区域”为 0）
    """
    if not ip:
        return None
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
        return None

    searcher = _get_ip2region_searchers().get(ip_obj.version)
    if searcher is None:
        return None
    try:
        raw = searcher.search(ip)
    except Exception:
        return None
    if not raw:
        return None

    parts = raw.split('|')
    if len(parts) < 3:
        return None
    if parts[1] == '0' and len(parts) >= 5:
        # 旧版 5 段格式
        province, city, isp = parts[2], parts[3], parts[4]
    else:
        province = parts[1]
        city = parts[2] if len(parts) > 2 else ''
        isp = parts[3] if len(parts) > 3 else ''
    return {
        'country': parts[0],
        'province': province,
        'city': city,
        'isp': isp,
        'raw': raw,
    }

def ip_region_matches_scope(region, scope):
    """判断归属地是否属于指定统计口径。

    全球统计所有 IP，不依赖归属地；全国只统计中国境内 IP
    （ip2region 中港澳台的国家字段同样是“中国”），境外 IP、内网/非法 IP、
    查不到归属地的 IP 都不计入。
    """
    if scope == 'global':
        return True
    if scope == 'nationwide':
        return bool(region) and region['country'] == '中国'
    if not region:
        return False
    if scope == 'zhejiang':
        return region['country'] == '中国' and region['province'] == '浙江省'
    if scope == 'hangzhou':
        return (region['country'] == '中国'
                and region['province'] == '浙江省'
                and region['city'] == '杭州市')
    return False

def count_visitors_by_scope(ips):
    """按 全球 / 全国 / 浙江省 / 杭州市 统计独立 IP 数。"""
    counts = {scope: 0 for scope in VISITOR_SCOPES}
    for ip in ips:
        region = lookup_ip_region(ip)
        if ip_region_matches_scope(region, 'global'):
            counts['global'] += 1
        if ip_region_matches_scope(region, 'nationwide'):
            counts['nationwide'] += 1
        if ip_region_matches_scope(region, 'zhejiang'):
            counts['zhejiang'] += 1
        if ip_region_matches_scope(region, 'hangzhou'):
            counts['hangzhou'] += 1
    return counts

def format_ip_region(region):
    """把归属地拼成便于展示的文本，如“中国 · 浙江省 · 杭州市 | 电信”。"""
    if not region:
        return ''
    labels = []
    for value in (region['country'], region['province'], region['city']):
        if not value or value == '0' or (labels and value == labels[-1]):
            continue
        labels.append(value)
    text = ' · '.join(labels)
    if region['isp'] and region['isp'] != '0':
        text = (text + ' | ' if text else '') + region['isp']
    return text

@app.before_request
def track_visit():
    if request.method != 'GET' or request.path.startswith(('/api/', '/static/', '/admin')):
        return
    ua = request.headers.get('User-Agent', '')
    if not ua or BOT_UA_PATTERN.search(ua):
        return
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO visits (path, ip, user_agent, user_id, timestamp) VALUES (?, ?, ?, ?, ?)',
        (request.path, get_client_ip(), ua[:255], session.get('user_id'), int(time.time() * 1000))
    )
    conn.commit()
    conn.close()

# 路由：前台页面
@app.route('/')
def index():
    initial_posts = query_posts('全部', session.get('user_id'))
    # 转成 JSON 字符串直接嵌入首屏 HTML，避免页面打开后再多一次 /api/posts 网络往返
    initial_posts_json = json.dumps(initial_posts, ensure_ascii=False).replace('</', '<\\/')
    return render_template('index.html', admin_contact=ADMIN_CONTACT, initial_posts_json=initial_posts_json)


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

# 查询帖子列表（首页服务端渲染和 /api/posts 共用）
def query_posts(category='全部', viewer_id=None):
    conn = get_db()
    cursor = conn.cursor()

    if category != '全部':
        cursor.execute('''
            SELECT p.*, u.username as author, u.avatar_url as author_avatar_url,
                (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) as comment_count
            FROM posts p LEFT JOIN users u ON p.user_id = u.id
            WHERE p.category = ? AND (p.is_public = 1 OR p.user_id = ?)
            ORDER BY COALESCE(p.updated_at, p.timestamp) DESC
        ''', (category, viewer_id))
    else:
        cursor.execute('''
            SELECT p.*, u.username as author, u.avatar_url as author_avatar_url,
                (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) as comment_count
            FROM posts p LEFT JOIN users u ON p.user_id = u.id
            WHERE (p.is_public = 1 OR p.user_id = ?)
            ORDER BY COALESCE(p.updated_at, p.timestamp) DESC
        ''', (viewer_id,))

    posts = []
    for row in cursor.fetchall():
        post = {
            'id': row['id'],
            'category': row['category'],
            'title': row['title'],
            'content': row['content'],
            'contact': row['contact'],
            'images': json.loads(row['images']) if row['images'] else [],
            'videos': json.loads(row['videos']) if row['videos'] else [],
            'timestamp': row['timestamp'],
            'updated_at': row['updated_at'] if 'updated_at' in row.keys() else None,
            'price': row['price'] if 'price' in row.keys() else None,
            'location': row['location'] if 'location' in row.keys() else None,
            'author': row['author'] if 'author' in row.keys() else None,
            'author_avatar_url': get_avatar_url(row['author_avatar_url']) if 'author_avatar_url' in row.keys() else DEFAULT_AVATAR_URL,
            'user_id': row['user_id'],
            'comment_count': row['comment_count'] if 'comment_count' in row.keys() else 0,
            'is_public': row['is_public'] if 'is_public' in row.keys() else 1
        }
        posts.append(post)

    conn.close()
    return posts

# API：获取所有信息
@app.route('/api/posts', methods=['GET'])
def get_posts():
    category = request.args.get('category', '全部')
    posts = query_posts(category, session.get('user_id'))
    return jsonify({'success': True, 'data': posts})

# API：获取单条信息详情
@app.route('/api/posts/<int:post_id>', methods=['GET'])
def get_post(post_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.*, u.username as author, u.avatar_url as author_avatar_url
        FROM posts p LEFT JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'message': '信息不存在'}), 404

    is_public = row['is_public'] if 'is_public' in row.keys() else 1
    is_owner = session.get('user_id') == row['user_id']
    is_admin = session.get('role') == 'admin'
    if not is_public and not (is_owner or is_admin):
        return jsonify({'success': False, 'message': '信息不存在'}), 404

    post = {
        'id': row['id'],
        'category': row['category'],
        'title': row['title'],
        'content': row['content'],
        'contact': row['contact'],
        'images': json.loads(row['images']) if row['images'] else [],
        'videos': json.loads(row['videos']) if row['videos'] else [],
        'timestamp': row['timestamp'],
        'updated_at': row['updated_at'] if 'updated_at' in row.keys() else None,
        'price': row['price'] if 'price' in row.keys() else None,
        'location': row['location'] if 'location' in row.keys() else None,
        'author': row['author'] if 'author' in row.keys() else None,
        'author_avatar_url': get_avatar_url(row['author_avatar_url']) if 'author_avatar_url' in row.keys() else DEFAULT_AVATAR_URL,
        'user_id': row['user_id'],
        'is_public': is_public
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
        session['avatar_url'] = user['avatar_url']
        session['bio'] = user['bio']
        session.permanent = True
        return jsonify({
            'success': True,
            'message': '登录成功',
            'user': {
                'id': user['id'],
                'username': user['username'],
                'role': user['role'],
                'avatar_url': get_avatar_url(user['avatar_url']),
                'bio': user['bio'] or ''
            }
        })

    return jsonify({'success': False, 'message': error}), 401

@app.route('/api/login/send-code', methods=['POST'])
def send_login_code():
    data = request.get_json(silent=True) or {}
    phone = (data.get('phone') or '').strip()
    if not valid_phone(phone):
        return jsonify({'success': False, 'message': '请输入有效的中国大陆手机号'}), 400

    now = int(time.time())
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT status FROM users WHERE phone = ?', (phone,))
    user = cursor.fetchone()
    if user and user['status'] == 'disabled':
        conn.close()
        return jsonify({'success': False, 'message': '账号已被禁用'}), 403
    cursor.execute('SELECT last_sent_at FROM sms_login_codes WHERE phone = ?', (phone,))
    previous = cursor.fetchone()
    if previous and now - previous['last_sent_at'] < 60:
        conn.close()
        return jsonify({'success': False, 'message': '验证码发送过于频繁，请稍后再试'}), 429

    try:
        out_id = send_aliyun_sms(phone)
    except Exception as exc:
        app.logger.warning('SMS send failed: %s', exc)
        conn.close()
        return jsonify({'success': False, 'message': str(exc) if isinstance(exc, RuntimeError) else '短信发送失败，请稍后重试'}), 503

    cursor.execute('''
        INSERT INTO sms_login_codes (phone, code_hash, out_id, expires_at, last_sent_at, attempts)
        VALUES (?, '', ?, ?, ?, 0)
        ON CONFLICT(phone) DO UPDATE SET code_hash='', out_id=excluded.out_id,
            expires_at=excluded.expires_at, last_sent_at=excluded.last_sent_at, attempts=0
    ''', (phone, out_id, now + 300, now))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '验证码已发送，请注意查收'})

@app.route('/api/login/sms', methods=['POST'])
def login_by_sms():
    data = request.get_json(silent=True) or {}
    phone = (data.get('phone') or '').strip()
    code = (data.get('code') or '').strip()
    if not valid_phone(phone) or not re.fullmatch(r'\d{4,6}', code):
        return jsonify({'success': False, 'message': '手机号或验证码格式不正确'}), 400

    now = int(time.time())
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM sms_login_codes WHERE phone = ?', (phone,))
    saved = cursor.fetchone()
    if not saved or saved['expires_at'] < now or not saved['out_id'] or saved['attempts'] >= 5:
        cursor.execute('DELETE FROM sms_login_codes WHERE phone = ?', (phone,))
        conn.commit()
        conn.close()
        return jsonify({'success': False, 'message': '验证码无效或已过期，请重新获取'}), 401
    try:
        verified = verify_aliyun_sms(phone, code, saved['out_id'])
    except Exception as exc:
        if 'isv.ValidateFail' in str(exc):
            cursor.execute('UPDATE sms_login_codes SET attempts = attempts + 1 WHERE phone = ?', (phone,))
            conn.commit()
            conn.close()
            return jsonify({'success': False, 'message': '验证码错误、已过期或已被新验证码替代，请获取最新验证码'}), 401
        app.logger.warning('SMS verification failed: %s', exc)
        conn.close()
        return jsonify({'success': False, 'message': '验证码核验服务暂不可用，请稍后重试'}), 503
    if not verified:
        cursor.execute('UPDATE sms_login_codes SET attempts = attempts + 1 WHERE phone = ?', (phone,))
        conn.commit()
        conn.close()
        return jsonify({'success': False, 'message': '验证码错误或已失效'}), 401

    cursor.execute('SELECT id, username, role, status, avatar_url, bio FROM users WHERE phone = ?', (phone,))
    user = cursor.fetchone()
    if user and user['status'] == 'disabled':
        conn.close()
        return jsonify({'success': False, 'message': '账号已被禁用'}), 403
    if not user:
        username = generate_default_username(cursor)
        password_hash = generate_password_hash(secrets.token_urlsafe(32))
        cursor.execute('''
            INSERT INTO users (username, password_hash, phone, role, status, created_at, last_login)
            VALUES (?, ?, ?, 'user', 'active', ?, ?)
        ''', (username, password_hash, phone, now * 1000, now * 1000))
        user_id = cursor.lastrowid
        cursor.execute('SELECT id, username, role, status, avatar_url, bio FROM users WHERE id = ?', (user_id,))
        user = cursor.fetchone()
    else:
        # Rename accounts created by the previous phone-derived username scheme.
        if user['username'] == f'p{phone}':
            username = generate_default_username(cursor)
            cursor.execute('UPDATE users SET username = ? WHERE id = ?', (username, user['id']))
        cursor.execute('UPDATE users SET last_login = ? WHERE id = ?', (now * 1000, user['id']))
        cursor.execute('SELECT id, username, role, status, avatar_url, bio FROM users WHERE id = ?', (user['id'],))
        user = cursor.fetchone()
    cursor.execute('DELETE FROM sms_login_codes WHERE phone = ?', (phone,))
    conn.commit()
    conn.close()

    session.clear()
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['avatar_url'] = user['avatar_url']
    session['bio'] = user['bio']
    session.permanent = True
    return jsonify({'success': True, 'message': '登录成功', 'user': {
        'id': user['id'], 'username': user['username'], 'role': user['role'],
        'avatar_url': get_avatar_url(user['avatar_url']), 'bio': user['bio'] or ''
    }})

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
                'role': session.get('role'),
                'avatar_url': get_avatar_url(session.get('avatar_url')),
                'bio': session.get('bio') or ''
            }
        })
    return jsonify({'success': True, 'logged_in': False})

# API：退出登录
@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True, 'message': '已退出登录'})

# API：更改当前用户的用户名
@app.route('/api/user/username', methods=['POST'])
@login_required
def update_username():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    valid, message = validate_username(username)
    if not valid:
        return jsonify({'success': False, 'message': message}), 400

    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT phone FROM users WHERE id = ?', (user_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        session.clear()
        return jsonify({'success': False, 'message': '用户不存在，请重新登录'}), 401
    if current['phone'] and current['phone'] in username:
        conn.close()
        return jsonify({'success': False, 'message': '用户名不能包含绑定的手机号'}), 400

    cursor.execute('SELECT 1 FROM users WHERE username = ? AND id != ?', (username, user_id))
    if cursor.fetchone():
        conn.close()
        return jsonify({'success': False, 'message': '用户名已被使用'}), 409

    cursor.execute('UPDATE users SET username = ? WHERE id = ?', (username, user_id))
    conn.commit()
    conn.close()
    session['username'] = username
    return jsonify({'success': True, 'message': '用户名已更新', 'username': username})

# API：设置当前用户的一句话介绍
@app.route('/api/user/bio', methods=['POST'])
@login_required
def update_bio():
    data = request.get_json(silent=True) or {}
    bio = ' '.join(str(data.get('bio') or '').split())
    if len(bio) > MAX_BIO_LENGTH:
        return jsonify({'success': False, 'message': f'一句话介绍不能超过{MAX_BIO_LENGTH}个字符'}), 400

    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM users WHERE id = ?', (user_id,))
    if not cursor.fetchone():
        conn.close()
        session.clear()
        return jsonify({'success': False, 'message': '用户不存在，请重新登录'}), 401

    cursor.execute('UPDATE users SET bio = ? WHERE id = ?', (bio or None, user_id))
    conn.commit()
    conn.close()

    session['bio'] = bio
    return jsonify({'success': True, 'message': '一句话介绍已更新', 'bio': bio})

# API：注销当前普通用户账号并清理关联数据
@app.route('/api/user/account', methods=['DELETE'])
@login_required
def delete_account():
    data = request.get_json(silent=True) or {}
    if data.get('confirmation') != '注销':
        return jsonify({'success': False, 'message': '请确认注销操作'}), 400

    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT role, phone, avatar_url FROM users WHERE id = ?', (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        session.clear()
        return jsonify({'success': False, 'message': '账号不存在'}), 404
    if user['role'] == 'admin':
        conn.close()
        return jsonify({'success': False, 'message': '管理员账号不能通过此入口注销'}), 403

    cursor.execute('SELECT images, videos FROM posts WHERE user_id = ?', (user_id,))
    media_urls = set()
    for post in cursor.fetchall():
        for field in ('images', 'videos'):
            try:
                values = json.loads(post[field]) if post[field] else []
            except (TypeError, json.JSONDecodeError):
                values = []
            if isinstance(values, list):
                media_urls.update(url for url in values if isinstance(url, str))
    if user['avatar_url']:
        media_urls.add(user['avatar_url'])
    cursor.execute('SELECT media_url FROM messages WHERE from_user_id = ? OR to_user_id = ?', (user_id, user_id))
    media_urls.update(row['media_url'] for row in cursor.fetchall() if row['media_url'])

    try:
        # Delete notifications referring to this user, their posts, or their comment threads.
        cursor.execute('''
            WITH RECURSIVE comment_tree(id) AS (
                SELECT id FROM comments WHERE user_id = ?
                UNION
                SELECT id FROM comments WHERE post_id IN (SELECT id FROM posts WHERE user_id = ?)
                UNION
                SELECT c.id FROM comments c JOIN comment_tree t ON c.parent_id = t.id
            )
            DELETE FROM notifications
            WHERE user_id = ? OR from_user_id = ?
               OR post_id IN (SELECT id FROM posts WHERE user_id = ?)
               OR comment_id IN (SELECT id FROM comment_tree)
        ''', (user_id, user_id, user_id, user_id, user_id))
        cursor.execute('DELETE FROM messages WHERE from_user_id = ? OR to_user_id = ?', (user_id, user_id))
        cursor.execute('''
            WITH RECURSIVE comment_tree(id) AS (
                SELECT id FROM comments WHERE user_id = ?
                UNION
                SELECT id FROM comments WHERE post_id IN (SELECT id FROM posts WHERE user_id = ?)
                UNION
                SELECT c.id FROM comments c JOIN comment_tree t ON c.parent_id = t.id
            )
            DELETE FROM comments WHERE id IN (SELECT id FROM comment_tree)
        ''', (user_id, user_id))
        cursor.execute('DELETE FROM posts WHERE user_id = ?', (user_id,))
        cursor.execute('DELETE FROM visits WHERE user_id = ?', (user_id,))
        if user['phone']:
            cursor.execute('DELETE FROM sms_login_codes WHERE phone = ?', (user['phone'],))
        cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))

        # Keep media still referenced by another profile, post, or message.
        still_referenced = get_referenced_media(cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        app.logger.exception('Account deletion failed for user_id=%s', user_id)
        return jsonify({'success': False, 'message': '注销失败，请稍后重试'}), 500
    conn.close()

    for url in media_urls - still_referenced:
        if url != DEFAULT_AVATAR_URL and OSS_PUBLIC_URL and url.startswith(OSS_PUBLIC_URL + '/'):
            delete_media(url)

    session.clear()
    return jsonify({'success': True, 'message': '账号已注销'})

# API：设置/更新头像（前端先通过 /api/presign type=avatar 直传OSS的Avatar/目录，再调用此接口保存URL）
@app.route('/api/user/avatar', methods=['POST'])
@login_required
def set_avatar():
    data = request.get_json()
    avatar_url = (data.get('avatar_url') or '').strip()

    if not avatar_url or not avatar_url.startswith(f"{OSS_PUBLIC_URL}/{AVATAR_OSS_FOLDER}/"):
        return jsonify({'success': False, 'message': '无效的头像地址'}), 400

    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT avatar_url FROM users WHERE id = ?', (user_id,))
    old = cursor.fetchone()
    cursor.execute('UPDATE users SET avatar_url = ? WHERE id = ?', (avatar_url, user_id))
    conn.commit()
    conn.close()

    # 删除旧的自定义头像文件
    if old and old['avatar_url'] and old['avatar_url'] != avatar_url:
        delete_media(old['avatar_url'])

    session['avatar_url'] = avatar_url
    return jsonify({'success': True, 'avatar_url': avatar_url})

# API：恢复默认头像
@app.route('/api/user/avatar', methods=['DELETE'])
@login_required
def reset_avatar():
    user_id = session['user_id']
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT avatar_url FROM users WHERE id = ?', (user_id,))
    old = cursor.fetchone()
    cursor.execute('UPDATE users SET avatar_url = NULL WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()

    if old and old['avatar_url']:
        delete_media(old['avatar_url'])

    session['avatar_url'] = None
    return jsonify({'success': True, 'avatar_url': DEFAULT_AVATAR_URL})

# API：发布信息（需要登录）
@app.route('/api/posts', methods=['POST'])
@login_required
def create_post():
    data = request.get_json()
    # 详细内容为选填：未传或为 null 时按空字符串存入（content 列为 NOT NULL）
    content = data.get('content') or ''

    conn = get_db()
    cursor = conn.cursor()

    images_json = json.dumps(data.get('images', []))
    videos_json = json.dumps(data.get('videos', []))

    cursor.execute('''
        INSERT INTO posts (category, title, content, contact, images, videos, timestamp, price, user_id, location)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('category'),
        data.get('title'),
        content,
        data.get('contact'),
        images_json,
        videos_json,
        data.get('timestamp'),
        data.get('price'),
        session.get('user_id'),
        data.get('location')
    ))

    conn.commit()
    post_id = cursor.lastrowid
    conn.close()

    notify_admin(
        f"新帖子：{data.get('title') or '(无标题)'}",
        f"分类：{data.get('category') or '-'}\n发布人：{session.get('username') or '-'}\n\n{content[:200]}"
    )

    new_post = {
        'id': post_id,
        'category': data.get('category'),
        'title': data.get('title'),
        'content': content,
        'contact': data.get('contact'),
        'images': data.get('images', []),
        'videos': data.get('videos', []),
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
    # 详细内容为选填：未传或为 null 时按空字符串处理
    content = data.get('content') or ''

    conn = get_db()
    cursor = conn.cursor()

    # 获取原有的图片和视频列表
    cursor.execute('SELECT images, videos FROM posts WHERE id = ?', (post_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return jsonify({'success': False, 'message': '信息不存在'}), 404

    old_images = json.loads(row['images']) if row['images'] else []
    old_videos = json.loads(row['videos']) if row['videos'] else []
    new_images = data.get('images', [])
    new_videos = data.get('videos', [])

    # 删除不再使用的媒体文件
    for media in old_images:
        if media not in new_images:
            delete_media(media)
    for media in old_videos:
        if media not in new_videos:
            delete_media(media)

    images_json = json.dumps(new_images)
    videos_json = json.dumps(new_videos)

    # 记录修改时间（保留原始发布时间 timestamp 不变），用于排序和显示"修改时间"
    now = int(time.time() * 1000)

    # 更新数据库记录
    cursor.execute('''
        UPDATE posts
        SET category = ?, title = ?, content = ?, contact = ?, images = ?, videos = ?, price = ?, location = ?, updated_at = ?
        WHERE id = ?
    ''', (
        data.get('category'),
        data.get('title'),
        content,
        data.get('contact'),
        images_json,
        videos_json,
        data.get('price'),
        data.get('location'),
        now,
        post_id
    ))

    conn.commit()
    conn.close()

    updated_post = {
        'id': post_id,
        'category': data.get('category'),
        'title': data.get('title'),
        'content': content,
        'contact': data.get('contact'),
        'images': new_images,
        'videos': new_videos,
        'timestamp': data.get('timestamp'),
        'updated_at': now,
        'price': data.get('price'),
        'location': data.get('location')
    }

    return jsonify({'success': True, 'message': '更新成功', 'data': updated_post})

# API：设置信息可见性（需要登录且是所有者或管理员）
@app.route('/api/posts/<int:post_id>/visibility', methods=['PUT'])
@post_owner_or_admin
def update_post_visibility(post_id):
    data = request.get_json()
    is_public = data.get('is_public')
    if is_public not in (0, 1):
        return jsonify({'success': False, 'message': '参数错误'}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE posts SET is_public = ? WHERE id = ?', (is_public, post_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': '设置成功', 'data': {'is_public': is_public}})

# API：获取我的信息
@app.route('/api/my_posts', methods=['GET'])
@login_required
def get_my_posts():
    user_id = session.get('user_id')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM posts WHERE user_id = ? ORDER BY COALESCE(updated_at, timestamp) DESC', (user_id,))

    posts = []
    for row in cursor.fetchall():
        post = {
            'id': row['id'],
            'category': row['category'],
            'title': row['title'],
            'content': row['content'],
            'contact': row['contact'],
            'images': json.loads(row['images']) if row['images'] else [],
            'videos': json.loads(row['videos']) if row['videos'] else [],
            'timestamp': row['timestamp'],
            'updated_at': row['updated_at'] if 'updated_at' in row.keys() else None,
            'price': row['price'] if 'price' in row.keys() else None,
            'user_id': row['user_id'],
            'location': row['location'] if 'location' in row.keys() else None,
            'is_public': row['is_public'] if 'is_public' in row.keys() else 1
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

    # Save media URLs before deleting the row; remove only files no longer used.
    cursor.execute('SELECT title, images, videos FROM posts WHERE id = ?', (post_id,))
    row = cursor.fetchone()
    media_urls = set()
    if row:
        for field in ('images', 'videos'):
            try:
                values = json.loads(row[field]) if row[field] else []
            except (TypeError, json.JSONDecodeError):
                values = []
            if isinstance(values, list):
                media_urls.update(url for url in values if isinstance(url, str))

    # 删除数据库记录
    cursor.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    conn.commit()
    still_referenced = get_referenced_media(cursor)
    conn.close()

    for media_url in media_urls - still_referenced:
        delete_media(media_url)

    if row:
        notify_admin(
            f"帖子被删除：{row['title'] or '(无标题)'}",
            f"操作人：{session.get('username') or '-'}\n帖子ID：{post_id}"
        )

    return jsonify({'success': True, 'message': '删除成功'})

# API：获取 OSS 预签名上传 URL（浏览器直传 OSS，绕过服务器）
@app.route('/api/presign', methods=['POST'])
@login_required
def presign_upload():
    data = request.get_json()
    original_filename = data.get('filename', '')
    client_content_type = data.get('content_type', '')
    upload_type = data.get('type', '')  # 'avatar' 时上传到 Avatar/ 目录，且仅允许图片

    if '.' not in original_filename:
        return jsonify({'success': False, 'message': '文件必须有扩展名'}), 400

    ext = original_filename.rsplit('.', 1)[1].lower()
    is_avatar = upload_type == 'avatar'
    allowed_set = ALLOWED_EXTENSIONS if is_avatar else ALLOWED_ALL_MEDIA
    if ext not in allowed_set:
        allowed = ', '.join(sorted(allowed_set))
        return jsonify({'success': False, 'message': f'不支持的文件格式，仅支持: {allowed}'}), 400

    is_video = ext in ALLOWED_VIDEO_EXTENSIONS
    content_type = client_content_type if client_content_type else get_content_type(ext)
    key = f"{AVATAR_OSS_FOLDER}/{uuid.uuid4().hex}.{ext}" if is_avatar else f"{uuid.uuid4().hex}.{ext}"

    try:
        bucket = get_oss_bucket()
        # 不签名 Content-Type，避免浏览器 OPTIONS 预检失败
        upload_url = bucket.sign_url('PUT', key, 3600, slash_safe=True)
        public_url = f"{OSS_PUBLIC_URL}/{key}"
        return jsonify({
            'success': True,
            'upload_url': upload_url,
            'public_url': public_url,
            'content_type': content_type,
            'is_video': is_video,
            'key': key
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'生成上传链接失败: {str(e)}'}), 500

# API：直传 OSS 后，服务端补写正确的 Content-Type，避免对象以
# application/octet-stream 返回时被浏览器当作附件下载。
@app.route('/api/fix_content_type', methods=['POST'])
@login_required
def fix_content_type():
    data = request.get_json(silent=True) or {}
    key = data.get('key', '')

    if not isinstance(key, str) or not re.match(r'^(?:Avatar/)?[0-9a-f]{32}\.[A-Za-z0-9]+$', key):
        return jsonify({'success': False, 'message': '无效的文件标识'}), 400

    ext = key.rsplit('.', 1)[1].lower()
    if ext not in ALLOWED_ALL_MEDIA:
        return jsonify({'success': False, 'message': '不支持的文件类型'}), 400

    content_type = get_content_type(ext)
    try:
        bucket = get_oss_bucket()
        bucket.update_object_meta(key, headers={'Content-Type': content_type, 'Content-Disposition': 'inline'})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 500

# API：上传图片（小文件兼容，仍保留供私信小图使用）
@app.route('/api/upload', methods=['POST'])
@login_required
def upload_image():
    try:
        file_key = 'file' if 'file' in request.files else 'image'
        if file_key not in request.files:
            return jsonify({'success': False, 'message': '未选择文件'}), 400

        file = request.files[file_key]
        original_filename = file.filename

        if not original_filename:
            return jsonify({'success': False, 'message': '未选择文件'}), 400
        if '.' not in original_filename:
            return jsonify({'success': False, 'message': '文件必须有扩展名'}), 400

        ext = original_filename.rsplit('.', 1)[1].lower()
        if ext not in ALLOWED_ALL_MEDIA:
            return jsonify({'success': False, 'message': '不支持的文件格式'}), 400

        is_video = ext in ALLOWED_VIDEO_EXTENSIONS
        filename = f"{uuid.uuid4().hex}.{ext}"
        content_type = get_content_type(ext)
        file_data = file.read()
        url = upload_to_oss(file_data, filename, content_type)
        return jsonify({'success': True, 'url': url, 'filename': url, 'is_video': is_video})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'上传失败: {str(e)}'}), 500

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

    # 获取用户列表及其发布数量，按最近活跃时间由近到远排序
    # 最近活跃 = 访问记录（visits 表）和登录时间（last_login）中较新的一个：
    # visits 表只从本次功能上线起才有数据，且不记录 /admin 页面的访问，单靠它排序会让老用户和站长账号排到最后
    cursor.execute('''
        SELECT
            u.id,
            u.username,
            u.role,
            u.status,
            u.created_at,
            u.last_login,
            (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id) as posts_count,
            (SELECT MAX(timestamp) FROM visits v WHERE v.user_id = u.id) as last_visit,
            MAX(COALESCE((SELECT MAX(timestamp) FROM visits v WHERE v.user_id = u.id), 0), COALESCE(u.last_login, 0)) as last_active
        FROM users u
        ORDER BY last_active DESC
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
            'posts_count': row['posts_count'],
            'last_visit': row['last_visit'],
            'last_active': row['last_active'] or None
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

    # 总访问量 / 独立访客数
    cursor.execute('SELECT COUNT(*) as count FROM visits')
    total_visits = cursor.fetchone()['count']

    cursor.execute('SELECT COUNT(DISTINCT ip) as count FROM visits')
    total_unique_visitors = cursor.fetchone()['count']

    # 今日访问量 / 独立访客数（最近24小时）
    cursor.execute('SELECT COUNT(*) as count FROM visits WHERE timestamp > ?', (today_timestamp,))
    visits_today = cursor.fetchone()['count']

    cursor.execute('SELECT COUNT(DISTINCT ip) as count FROM visits WHERE timestamp > ?', (today_timestamp,))
    unique_visitors_today = cursor.fetchone()['count']

    # 24 小时独立 IP 按地区拆分（ip2region 本地库）
    cursor.execute(
        "SELECT DISTINCT ip FROM visits WHERE timestamp > ? AND ip IS NOT NULL AND ip != ''",
        (today_timestamp,)
    )
    today_ips = [row['ip'] for row in cursor.fetchall()]
    visitor_counts_24h = count_visitors_by_scope(today_ips)

    conn.close()

    stats = {
        'total_users': total_users,
        'active_users': active_users,
        'disabled_users': disabled_users,
        'total_posts': total_posts,
        'posts_today': posts_today,
        'total_visits': total_visits,
        'total_unique_visitors': total_unique_visitors,
        'visits_today': visits_today,
        'unique_visitors_today': unique_visitors_today,
        'visitor_counts_24h': visitor_counts_24h,
        'ip_region_available': ip2region_available()
    }

    return jsonify({'success': True, 'data': stats})

# API：获取最近 24 小时独立访客列表（仅管理员）
# 支持 scope=global|nationwide|zhejiang|hangzhou，按 ip2region 归属地把 IP 分成
# 全球（全部 IP）/ 全国（仅中国境内）/ 浙江省 / 杭州市，境外及无法识别的 IP
# 只在全球口径中统计
@app.route('/api/admin/visitors/recent', methods=['GET'])
@admin_required
def get_recent_visitors():
    scope = request.args.get('scope', 'nationwide')
    if scope not in VISITOR_SCOPES:
        scope = 'nationwide'

    conn = get_db()
    cursor = conn.cursor()

    since = int(time.time() * 1000) - 24 * 60 * 60 * 1000
    # 不在 SQL 里 LIMIT：先按地区筛选，再对结果截取 500 条，避免筛掉目标地区的访客
    cursor.execute('''
        SELECT
            v.ip,
            COUNT(*) as visit_count,
            MIN(v.timestamp) as first_visit,
            MAX(v.timestamp) as last_visit,
            (
                SELECT u.username FROM visits v2
                JOIN users u ON u.id = v2.user_id
                WHERE v2.ip = v.ip AND v2.timestamp > ? AND v2.user_id IS NOT NULL
                ORDER BY v2.timestamp DESC LIMIT 1
            ) as username
        FROM visits v
        WHERE v.timestamp > ? AND v.ip IS NOT NULL AND v.ip != ''
        GROUP BY v.ip
        ORDER BY last_visit DESC
    ''', (since, since))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    counts = {name: 0 for name in VISITOR_SCOPES}
    matched = []
    for row in rows:
        region = lookup_ip_region(row['ip'])
        row['region_text'] = format_ip_region(region)
        in_scope = {name: ip_region_matches_scope(region, name) for name in VISITOR_SCOPES}
        for name, is_match in in_scope.items():
            if is_match:
                counts[name] += 1
        if in_scope[scope]:
            matched.append(row)

    visitors = matched[:500]

    return jsonify({
        'success': True,
        'scope': scope,
        'data': visitors,
        'counts': counts,
        'ip_region_available': ip2region_available()
    })

# API：获取有记录以来的全部独立访客列表（仅管理员）
@app.route('/api/admin/visitors/all', methods=['GET'])
@admin_required
def get_all_visitors():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT
            v.ip,
            COUNT(*) as visit_count,
            MIN(v.timestamp) as first_visit,
            MAX(v.timestamp) as last_visit,
            (
                SELECT u.username FROM visits v2
                JOIN users u ON u.id = v2.user_id
                WHERE v2.ip = v.ip AND v2.user_id IS NOT NULL
                ORDER BY v2.timestamp DESC LIMIT 1
            ) as username
        FROM visits v
        WHERE v.ip IS NOT NULL AND v.ip != ''
        GROUP BY v.ip
        ORDER BY visit_count DESC
        LIMIT 2000
    ''')

    visitors = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return jsonify({'success': True, 'data': visitors})

# API：查询 IP 归属地（仅管理员，按需调用，代理 ip-api.com 避免前端跨域/混合内容问题）
@app.route('/api/admin/ip-geo', methods=['GET'])
@admin_required
def get_ip_geo():
    ip = request.args.get('ip', '').strip()
    if not ip:
        return jsonify({'success': False, 'message': '缺少 ip 参数'}), 400

    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return jsonify({'success': False, 'message': 'IP 格式无效'}), 400

    if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
        return jsonify({'success': True, 'data': {'query': ip, 'status': 'fail', 'message': '内网/本地地址，无法查询归属地'}})

    try:
        resp = requests.get(
            f'http://ip-api.com/json/{ip}',
            params={'lang': 'zh-CN', 'fields': 'status,message,country,regionName,city,isp,org,query'},
            timeout=5
        )
        resp.raise_for_status()
        return jsonify({'success': True, 'data': resp.json()})
    except requests.RequestException as e:
        return jsonify({'success': False, 'message': f'查询失败: {str(e)}'}), 502

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

def _send_notification_email(title, desp=''):
    """实际发起通知邮件发送（在后台线程中执行，避免阻塞接口响应）"""
    try:
        msg = MIMEText(desp, 'plain', 'utf-8')
        msg['Subject'] = Header(title, 'utf-8')
        msg['From'] = SMTP_USER
        msg['To'] = ADMIN_EMAIL
        if SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            server.starttls()
        try:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [ADMIN_EMAIL], msg.as_string())
        finally:
            server.quit()
    except Exception as e:
        print(f'通知邮件发送失败: {e}')

def notify_admin(title, desp=''):
    """通过邮件通知站长（未配置 SMTP 时静默跳过）"""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and ADMIN_EMAIL):
        return
    threading.Thread(target=_send_notification_email, args=(title, desp), daemon=True).start()

def send_notification(cursor, user_id, ntype, post_id, comment_id, from_user_id, content):
    """发送通知（不重复通知自己）"""
    if user_id == from_user_id:
        return
    cursor.execute('''
        INSERT INTO notifications (user_id, type, post_id, comment_id, from_user_id, content, is_read, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
    ''', (user_id, ntype, post_id, comment_id, from_user_id, content, int(time.time() * 1000)))
    cleanup_notifications(cursor, user_id)

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
    cursor.execute('SELECT user_id, title FROM posts WHERE id = ?', (post_id,))
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

    notify_admin(
        f"新评论：{post_row['title'] or f'帖子 #{post_id}'}",
        f"评论人：{session.get('username') or '-'}\n\n{content}"
    )

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
    cursor.execute('''
        SELECT c.user_id, c.post_id, c.content, p.title AS post_title
        FROM comments c LEFT JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,))
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

    post_label = row['post_title'] or f"帖子 #{row['post_id']}"
    notify_admin(
        f"评论被删除：{post_label}",
        f"操作人：{session.get('username') or '-'}\n\n{row['content'] or ''}"
    )

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


# ========== 用户主页 ==========

# 路由：用户主页页面
@app.route('/user/<username>')
def user_profile(username):
    return render_template('user_profile.html', username=username)

# API：获取用户公开资料和帖子
@app.route('/api/users/<username>/profile', methods=['GET'])
def get_user_profile(username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, username, role, status, created_at, avatar_url, bio
        FROM users WHERE username = ? AND status = 'active'
    ''', (username,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': '用户不存在'}), 404

    is_self = session.get('user_id') == row['id']

    if is_self:
        cursor.execute('SELECT COUNT(*) as c FROM posts WHERE user_id = ?', (row['id'],))
    else:
        cursor.execute('SELECT COUNT(*) as c FROM posts WHERE user_id = ? AND is_public = 1', (row['id'],))
    posts_count = cursor.fetchone()['c']

    user_data = {
        'id': row['id'],
        'username': row['username'],
        'role': row['role'],
        'created_at': row['created_at'],
        'posts_count': posts_count,
        'avatar_url': get_avatar_url(row['avatar_url']),
        'bio': row['bio'] or ''
    }

    # 获取该用户的帖子（非本人只展示公开的帖子）
    if is_self:
        cursor.execute('''
            SELECT p.id, p.category, p.title, p.content, p.images, p.videos,
                   p.timestamp, p.updated_at, p.price, p.location, p.is_public,
                   (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) as comment_count
            FROM posts p WHERE p.user_id = ?
            ORDER BY COALESCE(p.updated_at, p.timestamp) DESC LIMIT 50
        ''', (row['id'],))
    else:
        cursor.execute('''
            SELECT p.id, p.category, p.title, p.content, p.images, p.videos,
                   p.timestamp, p.updated_at, p.price, p.location, p.is_public,
                   (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) as comment_count
            FROM posts p WHERE p.user_id = ? AND p.is_public = 1
            ORDER BY COALESCE(p.updated_at, p.timestamp) DESC LIMIT 50
        ''', (row['id'],))
    posts = []
    for p in cursor.fetchall():
        posts.append({
            'id': p['id'],
            'category': p['category'],
            'title': p['title'],
            'content': p['content'],
            'images': json.loads(p['images']) if p['images'] else [],
            'videos': json.loads(p['videos']) if p['videos'] else [],
            'timestamp': p['timestamp'],
            'updated_at': p['updated_at'],
            'price': p['price'],
            'location': p['location'],
            'comment_count': p['comment_count'],
            'is_public': p['is_public']
        })

    conn.close()
    return jsonify({'success': True, 'user': user_data, 'posts': posts})


# ========== 私信（DM）API ==========

# 路由：私信页面
@app.route('/dm')
def dm_list_page():
    return render_template('dm.html', target_username=None)

@app.route('/dm/<username>')
def dm_chat_page(username):
    return render_template('dm.html', target_username=username)

# API：获取我的会话列表
@app.route('/api/conversations', methods=['GET'])
@login_required
def get_conversations():
    me = session.get('user_id')
    conn = get_db()
    cursor = conn.cursor()

    # 第一步：取所有对话过的用户 ID，以及最后一条消息时间
    cursor.execute('''
        SELECT
            CASE WHEN from_user_id = ? THEN to_user_id ELSE from_user_id END as other_id,
            MAX(timestamp) as last_ts
        FROM messages
        WHERE from_user_id = ? OR to_user_id = ?
        GROUP BY other_id
        ORDER BY last_ts DESC
    ''', (me, me, me))
    rows = cursor.fetchall()

    conversations = []
    for r in rows:
        other_id = r['other_id']

        # 取用户名
        cursor.execute('SELECT username, avatar_url FROM users WHERE id = ?', (other_id,))
        u = cursor.fetchone()
        if not u:
            continue

        # 取最新一条消息内容
        cursor.execute('''
            SELECT content, content_type, timestamp FROM messages
            WHERE (from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?)
            ORDER BY timestamp DESC LIMIT 1
        ''', (me, other_id, other_id, me))
        last_msg = cursor.fetchone()

        # 取未读数
        cursor.execute('''
            SELECT COUNT(*) as cnt FROM messages
            WHERE from_user_id = ? AND to_user_id = ? AND is_read = 0
        ''', (other_id, me))
        unread = cursor.fetchone()['cnt']

        conversations.append({
            'user_id': other_id,
            'username': u['username'],
            'avatar_url': get_avatar_url(u['avatar_url']),
            'last_content': last_msg['content'] if last_msg else '',
            'last_type': last_msg['content_type'] if last_msg else 'text',
            'last_ts': r['last_ts'],
            'unread': unread
        })

    conn.close()
    return jsonify({'success': True, 'data': conversations})

# API：获取与某用户的消息记录
@app.route('/api/messages/<int:other_user_id>', methods=['GET'])
@login_required
def get_messages(other_user_id):
    me = session.get('user_id')
    conn = get_db()
    cursor = conn.cursor()

    # 标记对方发来的消息为已读
    cursor.execute('''
        UPDATE messages SET is_read = 1
        WHERE from_user_id = ? AND to_user_id = ? AND is_read = 0
    ''', (other_user_id, me))

    cursor.execute('''
        SELECT m.id, m.from_user_id, m.to_user_id, m.content,
               m.content_type, m.media_url, m.timestamp, m.is_read,
               u.username as from_username, u.avatar_url as from_avatar_url
        FROM messages m
        JOIN users u ON m.from_user_id = u.id
        WHERE (m.from_user_id = ? AND m.to_user_id = ?)
           OR (m.from_user_id = ? AND m.to_user_id = ?)
        ORDER BY m.timestamp ASC
        LIMIT 200
    ''', (me, other_user_id, other_user_id, me))

    rows = cursor.fetchall()
    conn.commit()
    conn.close()

    messages = [{
        'id': r['id'],
        'from_user_id': r['from_user_id'],
        'to_user_id': r['to_user_id'],
        'content': r['content'],
        'content_type': r['content_type'],
        'media_url': r['media_url'],
        'timestamp': r['timestamp'],
        'is_read': r['is_read'],
        'from_username': r['from_username'],
        'from_avatar_url': get_avatar_url(r['from_avatar_url'])
    } for r in rows]

    return jsonify({'success': True, 'data': messages})

# API：发送私信
@app.route('/api/messages/<int:other_user_id>', methods=['POST'])
@login_required
def send_message(other_user_id):
    me = session.get('user_id')

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE id = ? AND status = "active"', (other_user_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'success': False, 'message': '用户不存在'}), 404

    content_type = request.form.get('content_type', 'text')
    content = request.form.get('content', '').strip()
    media_url = request.form.get('media_url', '').strip() or None

    if content_type == 'text':
        if not content:
            conn.close()
            return jsonify({'success': False, 'message': '消息不能为空'}), 400
        if len(content) > 1000:
            conn.close()
            return jsonify({'success': False, 'message': '消息不能超过1000字'}), 400

    elif content_type in ('image', 'video'):
        # 前端已直传 OSS，media_url 由前端传入
        if not media_url:
            conn.close()
            return jsonify({'success': False, 'message': '缺少媒体文件URL'}), 400
        if not content:
            content = media_url.split('/')[-1]
    else:
        conn.close()
        return jsonify({'success': False, 'message': '不支持的消息类型'}), 400

    now = int(time.time() * 1000)
    cursor.execute('''
        INSERT INTO messages (from_user_id, to_user_id, content, content_type, media_url, timestamp, is_read)
        VALUES (?, ?, ?, ?, ?, ?, 0)
    ''', (me, other_user_id, content, content_type, media_url, now))
    msg_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'data': {
        'id': msg_id,
        'from_user_id': me,
        'to_user_id': other_user_id,
        'content': content,
        'content_type': content_type,
        'media_url': media_url,
        'timestamp': now,
        'is_read': 0,
        'from_username': session.get('username')
    }})

def cleanup_old_messages():
    """删除10天前的私信（媒体文件一并删除）"""
    expire_ts = int(time.time() * 1000) - 10 * 86400 * 1000
    conn = get_db()
    cursor = conn.cursor()
    # 先查出要删除的媒体文件
    cursor.execute('SELECT media_url FROM messages WHERE timestamp < ? AND media_url IS NOT NULL', (expire_ts,))
    for row in cursor.fetchall():
        delete_media(row['media_url'])
    cursor.execute('DELETE FROM messages WHERE timestamp < ?', (expire_ts,))
    conn.commit()
    conn.close()

def _cleanup_loop():
    """后台线程：每小时清理一次过期私信"""
    while True:
        try:
            cleanup_old_messages()
        except Exception as e:
            print(f"私信清理异常: {e}")
        time.sleep(3600)  # 每小时执行一次

# 启动后台清理线程
_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()

# API：获取未读私信总数
@app.route('/api/messages/unread_count', methods=['GET'])
@login_required
def get_dm_unread_count():
    me = session.get('user_id')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) as cnt FROM messages WHERE to_user_id = ? AND is_read = 0', (me,))
    count = cursor.fetchone()['cnt']
    conn.close()
    return jsonify({'success': True, 'count': count})


if __name__ == '__main__':
    os.makedirs('templates', exist_ok=True)

    # 初始化数据库
    init_db()

    # 配置 OSS CORS（允许网站直传文件）
    setup_oss_cors()

    # DEBUG_MODE = True     # 改成 False 即可切换为 waitress 生产模式运行

    DEBUG_MODE = False
    
         # 改成 False 即可切换为 waitress 生产模式运行
    if DEBUG_MODE:
        app.run(
            debug=True,
            host='0.0.0.0',
            port=5002, 
            threaded=True
        )
    else:
        from waitress import serve
        # 必须告诉 waitress 本机 Caddy 是可信代理，否则它会把 X-Forwarded-For 整个丢掉，
        # 我们永远拿不到真实访客 IP（见 get_client_ip 注释）。
        # trusted_proxy_count=1 表示代理链上只有 Caddy 一跳，解析时取 XFF 最后一段，
        # 也就是 Caddy 追加的真实客户端 IP，客户端自己伪造的前几段不会被采信。
        try:
            serve(app, host='0.0.0.0', port=5002, threads=8,
                  trusted_proxy='127.0.0.1',
                  trusted_proxy_count=1,
                  trusted_proxy_headers={'x-forwarded-for'},
                  # 保留 XFF 头本身不清理：万一代理回环地址不是 127.0.0.1（如走 ::1），
                  # get_client_ip 里的兜底分支仍能取到；远端直连 5002 的请求不受影响
                  clear_untrusted_proxy_headers=False)
        except TypeError:
            # waitress 1.x 不认识这些代理参数；它本来也不清理 X-Forwarded-For，
            # get_client_ip 的兜底分支依然能拿到真实 IP，所以退回原样启动即可
            serve(app, host='0.0.0.0', port=5002, threads=8)
