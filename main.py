from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    make_response,
    redirect,
    url_for,
)
from flask_bootstrap import Bootstrap5
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from dotenv import load_dotenv
import os
import ipaddress
import subprocess
import requests
import threading
import time

load_dotenv()

app = Flask(__name__)

bootstrap = Bootstrap5(app)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY", "bd2ba57e-cf1b-44c9-bbf2-217d5c9a37b6"
)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///Database.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

config_files_path = os.getenv("CONFIG_FILES_PATH", "./configs")


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


development_mode = env_flag("DEVELOPMENT", False)
pterodactyl_node_id = os.getenv("PTERODACTYL_NODE_ID", "6")


def proxy_filename(ip, port, protocol):
    ip_filename = ip.replace(".", "-")
    return f"{ip_filename}_{port}_{protocol}.conf"


def parse_proxy_filename(filename):
    parts = filename.split("_")
    if len(parts) != 3 or not filename.endswith(".conf"):
        return None

    return {
        "ip": parts[0].replace("-", "."),
        "port": parts[1],
        "protocol": parts[2][:-5],
        "filename": filename,
    }


def validate_proxy_data(ip, port, protocol):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False, "Invalid IP address"

    if not str(port).isdigit():
        return False, "Invalid port"

    protocol_lower = protocol.lower()
    if protocol_lower not in {"tcp", "udp", "both"}:
        return False, "Invalid protocol (must be tcp, udp, or both)"

    return True, None


def build_proxy_config(ip, port, protocol):
    p = protocol.lower()
    if p == "both":
        return (
            f"server {{{{ listen {port}; proxy_pass {ip}:{port}; }}}}\n"
            f"server {{{{ listen {port} udp; proxy_pass {ip}:{port}; }}}}\n"
        )
    udp_suffix = " udp" if p == "udp" else ""
    return f"server {{{{ listen {port}{udp_suffix}; proxy_pass {ip}:{port}; }}}}\n"


def test_and_reload_nginx():
    if development_mode:
        return True, None

    result = subprocess.run(["nginx", "-t"], capture_output=True)
    if result.returncode == 0:
        os.system("nginx -s reload")
        return True, None

    error_message = result.stderr.decode().strip()
    return False, error_message or "nginx -t failed"


def get_allocations_from_pterodactyl():
    url = f"{os.getenv('PTERODACTYL_API_URL')}/application/nodes/{pterodactyl_node_id}/allocations"
    key = os.getenv("PTERODACTYL_API_KEY")
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception("Failed to fetch allocations from Pterodactyl API")
    return response.json().get("data", [])


def parse_allocation(allocation):
    return {
        "id": allocation["attributes"]["id"],
        "ip": allocation["attributes"]["ip"],
        "port": allocation["attributes"]["port"],
        "notes": allocation["attributes"]["notes"],
    }


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("jwt_token")

        if not token:
            return jsonify({"message": "Token is missing!"}), 401

        try:
            data = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
            current_user = User.query.filter_by(public_id=data["public_id"]).first()
        except jwt.PyJWTError:
            return jsonify({"message": "Token is invalid!"}), 401

        return f(current_user, *args, **kwargs)

    return decorated


def create_allocation(ip, port):
    url = f"{os.getenv('PTERODACTYL_API_URL')}/application/nodes/{pterodactyl_node_id}/allocations"
    key = os.getenv("PTERODACTYL_API_KEY")
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "ip": ip,
        "ports": [str(port)],
        "notes": "Created via Proxy Manager",
    }
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    if response.status_code not in {200, 201, 202, 204}:
        raise Exception(
            f"Failed to create allocation in Pterodactyl API: {response.status_code} {response.text}"
        )
    if response.status_code == 204 or not response.content:
        return {}
    return response.json().get("data", {})


def find_allocation_by_ip_port(ip, port):
    allocations = get_allocations_from_pterodactyl()
    port_str = str(port)

    for allocation in allocations:
        parsed = parse_allocation(allocation)
        if parsed["ip"] == ip and str(parsed["port"]) == port_str:
            return parsed

    return None


def delete_allocation(allocation_id):
    url = f"{os.getenv('PTERODACTYL_API_URL')}/application/nodes/{pterodactyl_node_id}/allocations/{allocation_id}"
    key = os.getenv("PTERODACTYL_API_KEY")
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    response = requests.delete(url, headers=headers)
    if response.status_code not in {200, 204}:
        raise Exception("Failed to delete allocation in Pterodactyl API")


def sync_allocations():
    # Sync allocations from Pterodactyl API to local config files
    parsed_allocations = []
    try:
        allocations = get_allocations_from_pterodactyl()
        for allocation in allocations:
            parsed = parse_allocation(allocation)
            parsed_allocations.append(parsed)
            filename = proxy_filename(parsed["ip"], parsed["port"], "tcp")
            filename_udp = proxy_filename(parsed["ip"], parsed["port"], "udp")
            filename_both = proxy_filename(parsed["ip"], parsed["port"], "both")
            file_path = os.path.join(config_files_path, filename)
            file_path_udp = os.path.join(config_files_path, filename_udp)
            file_path_both = os.path.join(config_files_path, filename_both)
            if (
                not os.path.exists(file_path)
                and not os.path.exists(file_path_udp)
                and not os.path.exists(file_path_both)
            ):
                content = build_proxy_config(parsed["ip"], parsed["port"], "tcp")
                with open(file_path, "w", encoding="utf-8") as config_file:
                    config_file.write(content)
        test_and_reload_nginx()
    except Exception as e:
        print(f"Error syncing allocations: {e}")

    # Sync local config files to Pterodactyl API
    try:
        items = [
            parse_proxy_filename(item)
            for item in os.listdir(config_files_path)
            if item.endswith(".conf")
        ]
        # Remove dupe entries keeping tcp over udp if both exist for same ip:port
        items = sorted(
            items,
            key=lambda x: (x["ip"], x["port"], 0 if x["protocol"] == "tcp" else 1),
        )
        unique_items = []
        seen = set()
        for item in items:
            key = (item["ip"], item["port"])
            if key not in seen:
                unique_items.append(item)
                seen.add(key)
        items = unique_items

        print(
            f"Found {len(items)} proxy config files locally and {len(parsed_allocations)} allocations from Pterodactyl API"
        )
        for item in unique_items:
            if not any(
                alloc["ip"] == item["ip"] and str(alloc["port"]) == item["port"]
                for alloc in parsed_allocations
            ):
                create_allocation(item["ip"], item["port"])

    except Exception as e:
        print(f"Error syncing config files: {e}")


def hourly_sync_loop(interval_seconds=3600):
    while True:
        time.sleep(interval_seconds)
        try:
            sync_allocations()
        except Exception as e:
            print(f"Hourly sync failed: {e}")


def start_hourly_sync_task():
    thread = threading.Thread(target=hourly_sync_loop, daemon=True)
    thread.start()


def list_nodes_from_pterodactyl():
    url = f"{os.getenv('PTERODACTYL_API_URL')}/application/nodes"
    key = os.getenv("PTERODACTYL_API_KEY")
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception("Failed to fetch nodes from Pterodactyl API")
    return response.json().get("data", [])


@app.route("/")
def index():
    token = request.cookies.get("jwt_token")
    if not token:
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route("/api/proxies", methods=["GET"])
@token_required
def list_items(current_user):
    items = [
        parse_proxy_filename(item)
        for item in os.listdir(config_files_path)
        if item.endswith(".conf")
    ]
    items = [item for item in items if item is not None]
    return jsonify(items)


@app.route("/api/nodes", methods=["GET"])
@token_required
def list_nodes(current_user):
    try:
        nodes = list_nodes_from_pterodactyl()

        # Add nodes to local database if not exist
        for node in nodes:
            try:
                existing_node = Node.query.filter_by(
                    id=node["attributes"]["id"]
                ).first()
                if not existing_node:
                    new_node = Node(
                        id=node["attributes"]["id"],
                        name=node["attributes"]["name"],
                        fqdn=node["attributes"]["fqdn"],
                        ip_address=None,
                    )
                    db.session.add(new_node)
            except Exception as e:
                print(f"Error processing node {node['attributes']['id']}: {e}")
        db.session.commit()

        # Get all nodes from local database to ensure we have a complete list
        nodes = Node.query.all()

        # Return nodes with IP addresses (if assigned) or null if not assigned

        return jsonify(
            [
                {
                    "id": node.id,
                    "name": node.name,
                    "fqdn": node.fqdn,
                    "ip_address": node.ip_address,
                }
                for node in nodes
            ]
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update_node_ip", methods=["POST"])
@token_required
def update_node_ip(current_user):
    payload = request.get_json(silent=True) or {}
    node_id = payload.get("id")
    raw_ip = payload.get("ip")

    if node_id is None or node_id == "":
        return jsonify({"error": "Node ID is required"}), 400

    ip_address = None
    if raw_ip is not None:
        ip_text = str(raw_ip).strip()
        if ip_text:
            try:
                ipaddress.ip_address(ip_text)
            except ValueError:
                return jsonify({"error": "Invalid IP address"}), 400
            ip_address = ip_text

    try:
        node = Node.query.filter_by(id=node_id).first()
        if not node:
            return jsonify({"error": "Node not found"}), 404

        node.ip_address = ip_address
        db.session.commit()

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/add", methods=["POST"])
@token_required
def add_proxy(current_user):
    payload = request.get_json(silent=True) or {}
    ip = payload.get("ip", "").strip()
    port = str(payload.get("port", "")).strip()
    protocol = payload.get("protocol", "").strip().lower()

    is_valid, error = validate_proxy_data(ip, port, protocol)
    if not is_valid:
        return jsonify({"error": error}), 400

    filename = proxy_filename(ip, port, protocol)
    file_path = os.path.join(config_files_path, filename)
    if os.path.exists(file_path):
        return jsonify({"error": "Proxy config already exists"}), 409

    # Prevent overlap: "both" conflicts with tcp/udp files and vice versa
    if protocol == "both":
        for alt in ("tcp", "udp"):
            alt_path = os.path.join(config_files_path, proxy_filename(ip, port, alt))
            if os.path.exists(alt_path):
                return jsonify(
                    {"error": f"A separate {alt} config already exists for {ip}:{port}"}
                ), 409
    else:
        both_path = os.path.join(config_files_path, proxy_filename(ip, port, "both"))
        if os.path.exists(both_path):
            return jsonify(
                {"error": f"A tcp & udp config already exists for {ip}:{port}"}
            ), 409

    os.makedirs(config_files_path, exist_ok=True)
    content = build_proxy_config(ip, port, protocol)
    with open(file_path, "w", encoding="utf-8") as config_file:
        config_file.write(content)

    success, error_message = test_and_reload_nginx()
    if not success:
        if os.path.exists(file_path):
            os.remove(file_path)
            test_and_reload_nginx()
        return jsonify({"error": f"Nginx reload failed: {error_message}"}), 500

    try:
        existing_allocation = find_allocation_by_ip_port(ip, port)
        if not existing_allocation:
            create_allocation(ip, port)
    except Exception as e:
        rollback_error = None
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
            rollback_ok, rollback_nginx_error = test_and_reload_nginx()
            if not rollback_ok:
                rollback_error = rollback_nginx_error
        except Exception as rollback_exception:
            rollback_error = str(rollback_exception)

        if rollback_error:
            return jsonify(
                {
                    "error": f"Failed to create allocation: {e}",
                    "rollback_error": rollback_error,
                }
            ), 502

        return jsonify({"error": f"Failed to create allocation: {e}"}), 502

    return jsonify({"ok": True})


@app.route("/api/remove", methods=["POST"])
@token_required
def remove_proxy(current_user):
    payload = request.get_json(silent=True) or {}
    ip = payload.get("ip", "").strip()
    port = str(payload.get("port", "")).strip()
    protocol = payload.get("protocol", "").strip().lower()

    is_valid, error = validate_proxy_data(ip, port, protocol)
    if not is_valid:
        return jsonify({"error": error}), 400

    filename = proxy_filename(ip, port, protocol)
    file_path = os.path.join(config_files_path, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "Proxy config not found"}), 404

    with open(file_path, "r", encoding="utf-8") as config_file:
        previous_content = config_file.read()

    os.remove(file_path)

    success, error_message = test_and_reload_nginx()
    if not success:
        with open(file_path, "w", encoding="utf-8") as config_file:
            config_file.write(previous_content)
        test_and_reload_nginx()
        return jsonify({"error": f"Nginx reload failed: {error_message}"}), 500

    try:
        allocation = find_allocation_by_ip_port(ip, port)
        if allocation:
            delete_allocation(allocation["id"])
    except Exception as e:
        rollback_error = None
        try:
            with open(file_path, "w", encoding="utf-8") as config_file:
                config_file.write(previous_content)
            rollback_ok, rollback_nginx_error = test_and_reload_nginx()
            if not rollback_ok:
                rollback_error = rollback_nginx_error
        except Exception as rollback_exception:
            rollback_error = str(rollback_exception)

        if rollback_error:
            return jsonify(
                {
                    "error": f"Failed to delete allocation: {e}",
                    "rollback_error": rollback_error,
                }
            ), 500

        return jsonify({"error": f"Failed to delete allocation: {e}"}), 500

    return jsonify({"ok": True})


@app.route("/api/edit", methods=["POST"])
@token_required
def edit_proxy(current_user):
    payload = request.get_json(silent=True) or {}

    old_ip = payload.get("old_ip", "").strip()
    old_port = str(payload.get("old_port", "")).strip()
    old_protocol = payload.get("old_protocol", "").strip().lower()
    new_ip = payload.get("new_ip", "").strip()
    new_port = str(payload.get("new_port", "")).strip()
    new_protocol = payload.get("new_protocol", "").strip().lower()

    is_valid_old, old_error = validate_proxy_data(old_ip, old_port, old_protocol)
    if not is_valid_old:
        return jsonify({"error": f"Old proxy invalid: {old_error}"}), 400

    is_valid_new, new_error = validate_proxy_data(new_ip, new_port, new_protocol)
    if not is_valid_new:
        return jsonify({"error": f"New proxy invalid: {new_error}"}), 400

    old_filename = proxy_filename(old_ip, old_port, old_protocol)
    new_filename = proxy_filename(new_ip, new_port, new_protocol)

    old_path = os.path.join(config_files_path, old_filename)
    new_path = os.path.join(config_files_path, new_filename)

    if not os.path.exists(old_path):
        return jsonify({"error": "Original proxy config not found"}), 404

    if old_path != new_path and os.path.exists(new_path):
        return jsonify({"error": "Target proxy config already exists"}), 409

    # Prevent overlap: "both" conflicts with tcp/udp and vice versa
    if old_path != new_path:
        if new_protocol == "both":
            for alt in ("tcp", "udp"):
                alt_path = os.path.join(
                    config_files_path, proxy_filename(new_ip, new_port, alt)
                )
                if os.path.exists(alt_path) and alt_path != old_path:
                    return jsonify(
                        {
                            "error": f"A separate {alt} config already exists for {new_ip}:{new_port}"
                        }
                    ), 409
        else:
            both_path = os.path.join(
                config_files_path, proxy_filename(new_ip, new_port, "both")
            )
            if os.path.exists(both_path) and both_path != old_path:
                return jsonify(
                    {
                        "error": f"A tcp & udp config already exists for {new_ip}:{new_port}"
                    }
                ), 409

    with open(old_path, "r", encoding="utf-8") as config_file:
        old_content = config_file.read()

    def rollback_local_edit():
        if old_path != new_path:
            if os.path.exists(new_path):
                os.rename(new_path, old_path)
            with open(old_path, "w", encoding="utf-8") as rollback_file:
                rollback_file.write(old_content)
        else:
            with open(old_path, "w", encoding="utf-8") as rollback_file:
                rollback_file.write(old_content)

        rollback_ok, rollback_nginx_error = test_and_reload_nginx()
        if not rollback_ok:
            return rollback_nginx_error
        return None

    if old_path != new_path:
        os.rename(old_path, new_path)

    content = build_proxy_config(new_ip, new_port, new_protocol)
    with open(new_path, "w", encoding="utf-8") as config_file:
        config_file.write(content)

    success, error_message = test_and_reload_nginx()
    if not success:
        rollback_error = rollback_local_edit()
        if rollback_error:
            return jsonify(
                {
                    "error": f"Nginx reload failed: {error_message}",
                    "rollback_error": rollback_error,
                }
            ), 500
        return jsonify({"error": f"Nginx reload failed: {error_message}"}), 500

    created_new_allocation = False
    deleted_old_allocation = False
    try:
        old_pair = (old_ip, str(old_port))
        new_pair = (new_ip, str(new_port))

        if old_pair != new_pair:
            new_allocation = find_allocation_by_ip_port(new_ip, new_port)
            if not new_allocation:
                create_allocation(new_ip, new_port)
                created_new_allocation = True

            old_allocation = find_allocation_by_ip_port(old_ip, old_port)
            if old_allocation:
                delete_allocation(old_allocation["id"])
                deleted_old_allocation = True
    except Exception as e:
        rollback_error = rollback_local_edit()
        remote_rollback_error = None

        try:
            if old_pair != new_pair and deleted_old_allocation:
                old_allocation_now = find_allocation_by_ip_port(old_ip, old_port)
                if not old_allocation_now:
                    create_allocation(old_ip, old_port)

            if old_pair != new_pair and created_new_allocation:
                new_allocation_now = find_allocation_by_ip_port(new_ip, new_port)
                if new_allocation_now:
                    delete_allocation(new_allocation_now["id"])
        except Exception as remote_exception:
            remote_rollback_error = str(remote_exception)

        error_payload = {"error": f"Failed to update allocation: {e}"}
        if rollback_error:
            error_payload["rollback_error"] = rollback_error
        if remote_rollback_error:
            error_payload["remote_rollback_error"] = remote_rollback_error
        return jsonify(error_payload), 500

    return jsonify({"ok": True})


# Node IP Assignments
class Node(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)
    fqdn = db.Column(db.String(255), unique=True)
    ip_address = db.Column(db.String(45), unique=True, nullable=True)


# Authentication
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(70), unique=True)
    password = db.Column(db.String(80))


def initialize_auth_data():
    with app.app_context():
        db.create_all()

        admin_email = os.getenv("ADMIN_USERNAME", "admin")
        if User.query.filter_by(email=admin_email).first():
            return

        hashed_password = generate_password_hash(os.getenv("ADMIN_PASSWORD", "admin"))
        new_user = User(
            public_id=str(uuid.uuid4()),
            name="Admin",
            email=admin_email,
            password=hashed_password,
        )
        db.session.add(new_user)
        db.session.commit()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password, password):
            return jsonify({"message": "Invalid email or password"}), 401

        token = jwt.encode(
            {
                "public_id": user.public_id,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        response = make_response(redirect(url_for("index")))
        response.set_cookie("jwt_token", token)

        return response

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    response = make_response(redirect(url_for("login")))
    response.set_cookie("jwt_token", "", expires=0)
    return response


@app.route("/api/allocations", methods=["GET"])
@token_required
def list_allocations(current_user):
    # Get allocations from Pterodactyl API
    try:
        allocations = get_allocations_from_pterodactyl()
        data = [parse_allocation(allocation) for allocation in allocations]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(data)


# Startup tasks
initialize_auth_data()
sync_allocations()
if os.getenv("WERKZEUG_RUN_MAIN") != "false":
    start_hourly_sync_task()

if __name__ == "__main__":
    app.run()
