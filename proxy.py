import os
import subprocess

def update_proxy(port, target_ip):
    config_path = f"/etc/nginx/streams-enabled/port_{port}.conf"
    config_content = f"server {{ listen {port}; proxy_pass {target_ip}:{port}; }}\n"
    
    # Write the new config file
    with open(config_path, "w") as f:
        f.write(config_content)
    
    # Test and Reload
    # -t checks for syntax errors before applying
    result = subprocess.run(["nginx", "-t"], capture_output=True)
    if result.returncode == 0:
        os.system("nginx -s reload")
    else:
        print(f"Error in config: {result.stderr.decode()}")