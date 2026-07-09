import os
import re
import shutil
import sys
import hmac
import hashlib
from datetime import datetime
from urllib.parse import urlparse

import uvicorn
import docker
from github import Github
import git
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse

# Load environment variables from .env file
load_dotenv()

# Extend PATH to include Rancher Desktop and Homebrew binaries if they exist
for path in [os.path.expanduser("~/.rd/bin"), "/opt/homebrew/bin"]:
    if os.path.exists(path) and path not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{path}:{os.environ.get('PATH', '')}"


# --- Configuration ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

def parse_pr_url(pr_url: str):
    """Parses the GitHub PR URL to extract owner, repo, and PR number."""
    try:
        parsed_url = urlparse(pr_url)
        path_parts = parsed_url.path.strip("/").split("/")
        
        if len(path_parts) >= 4 and path_parts[2] == "pull":
            owner = path_parts[0]
            repo_name = path_parts[1]
            pr_number = int(path_parts[3])
            return owner, repo_name, pr_number
        else:
            raise ValueError("Invalid PR URL format.")
    except (ValueError, IndexError) as e:
        print(f"Error: Could not parse PR URL '{pr_url}'. Please use a valid URL.")
        print("Example: https://github.com/owner/repo/pull/123")
        raise e

async def deploy_pr_logic(pr_url: str):
    """
    Generator function that clones a PR, builds a Docker image, 
    runs a container, and yields log output.
    """
    if not GITHUB_TOKEN:
        yield "Error: GITHUB_TOKEN not found in environment variables.\n"
        return

    clone_dir = None
    try:
        # --- 1. Parse URL and Fetch PR Info ---
        owner, repo_name, pr_number = parse_pr_url(pr_url)
        clone_dir = f"temp_repo_{pr_number}"
        yield f"Deploying PR #{pr_number} from {owner}/{repo_name}...\n"

        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(f"{owner}/{repo_name}")
        pr = repo.get_pull(pr_number)

        head_branch = pr.head.ref
        head_repo_url = pr.head.repo.clone_url
        
        yield f"Source branch: '{head_branch}' from repository '{pr.head.repo.full_name}'\n"

        # --- 2. Clone Repository and Checkout PR Branch ---
        if os.path.exists(clone_dir):
            yield f"Removing existing directory: {clone_dir}\n"
            shutil.rmtree(clone_dir)

        yield f"Cloning repository from {head_repo_url}...\n"
        cloned_repo = git.Repo.clone_from(head_repo_url, clone_dir, branch=head_branch)
        yield f"Successfully cloned and checked out branch '{head_branch}'.\n"

        # Define base URLs for standard and Rancher Desktop socket paths
        socket_paths = [
            "unix:///var/run/docker.sock",
            f"unix://{os.path.expanduser('~/.rd/docker.sock')}"
        ]
        
        docker_client = None
        for path in socket_paths:
            try:
                temp_client = docker.DockerClient(base_url=path)
                temp_client.ping()
                docker_client = temp_client
                yield f"Connected to Docker daemon at {path}\n"
                break
            except Exception:
                continue
                
        if not docker_client:
            try:
                docker_client = docker.from_env()
                docker_client.ping()
                yield "Connected to Docker daemon from environment.\n"
            except Exception as e:
                yield f"Error: Could not connect to Docker daemon. Please ensure Rancher Desktop or Docker Desktop is running. ({str(e)})\n"
                return

        # Find deployable components dynamically
        components = []
        
        # Check backend
        backend_rel = None
        for p in ["backend/Dockerfile", "Backend/Dockerfile"]:
            if os.path.exists(os.path.join(clone_dir, p)):
                backend_rel = p
                break
        if backend_rel:
            components.append({
                "name": "backend",
                "dockerfile": backend_rel,
                "host_port": 5000,
                "env": {"DB_HOST": "host.docker.internal"}
            })
            
        # Check frontend
        frontend_rel = None
        for p in ["frontend/Dockerfile", "Frontend/Dockerfile"]:
            if os.path.exists(os.path.join(clone_dir, p)):
                frontend_rel = p
                break
        if frontend_rel:
            components.append({
                "name": "frontend",
                "dockerfile": frontend_rel,
                "host_port": 8080,
                "env": {}
            })
            
        # Fallback to root Dockerfile if no subfolders found
        if not components:
            if os.path.exists(os.path.join(clone_dir, "Dockerfile")):
                components.append({
                    "name": "app",
                    "dockerfile": "Dockerfile",
                    "host_port": 8080,
                    "env": {"DB_HOST": "host.docker.internal"}
                })

        if not components:
            yield "Error: No Dockerfile found in the repository.\n"
            return

        for comp in components:
            comp_name = comp["name"]
            rel_dockerfile_path = comp["dockerfile"]
            host_port = comp["host_port"]
            container_env = comp["env"]
            
            image_tag = f"{repo_name.lower()}-{comp_name}-pr-{pr_number}"
            container_name = f"{repo_name}-{comp_name}-pr-{pr_number}-container"

            yield f"\n--- Starting deployment for {comp_name} ---\n"
            yield f"Building Docker image with tag: {image_tag}\n"

            # Try to find the EXPOSE port in the Dockerfile dynamically
            exposed_port = 8000  # Default fallback
            try:
                dockerfile_path = os.path.join(clone_dir, rel_dockerfile_path)
                with open(dockerfile_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    match = re.search(r"^\s*EXPOSE\s+(\d+)", content, re.MULTILINE | re.IGNORECASE)
                    if match:
                        exposed_port = int(match.group(1))
                        yield f"Detected exposed port in Dockerfile for {comp_name}: {exposed_port}\n"
            except Exception:
                pass

            # Stream build logs in real-time
            try:
                build_context_path = clone_dir
                build_logs = docker_client.api.build(
                    path=build_context_path,
                    dockerfile=rel_dockerfile_path,
                    tag=image_tag,
                    rm=True,
                    decode=True
                )
                for chunk in build_logs:
                    if 'stream' in chunk:
                        for line in chunk['stream'].splitlines():
                            yield line + '\n'
                    elif 'errorDetail' in chunk:
                        error_msg = chunk.get('error', 'Unknown build error')
                        yield f"Build error: {error_msg}\n"
                        return
            except Exception as e:
                yield f"Error initiating Docker build for {comp_name}: {str(e)}\n"
                return
                
            yield f"Successfully built image: {image_tag}\n"

            try:
                # Stop and remove container if it already exists
                existing_container = docker_client.containers.get(container_name)
                yield f"Stopping and removing existing container: {container_name}\n"
                existing_container.stop()
                existing_container.remove()
            except docker.errors.NotFound:
                pass # Container doesn't exist, which is fine

            extra_hosts = None
            if comp_name == "frontend":
                try:
                    backend_container_name = f"{repo_name}-backend-pr-{pr_number}-container"
                    backend_container = docker_client.containers.get(backend_container_name)
                    backend_ip = backend_container.attrs['NetworkSettings']['Networks']['bridge']['IPAddress']
                    if backend_ip:
                        extra_hosts = {"backend": backend_ip}
                        yield f"Found running backend container at IP: {backend_ip}. Mapping host 'backend' to this IP.\n"
                except Exception as e:
                    # Fallback to bridge gateway IP
                    try:
                        network = docker_client.networks.get("bridge")
                        configs = network.attrs.get("IPAM", {}).get("Config", [])
                        gateway_ip = configs[0].get("Gateway") if configs else "172.17.0.1"
                    except Exception:
                        gateway_ip = "172.17.0.1"
                    extra_hosts = {"backend": gateway_ip}
                    yield f"Warning: Could not resolve backend container IP ({e}). Falling back to bridge gateway: {gateway_ip}\n"

            yield f"Running Docker container '{container_name}'...\n"
            container = docker_client.containers.run(
                image_tag,
                detach=True,
                name=container_name,
                ports={f'{exposed_port}/tcp': host_port}, # Maps detected exposed port to component's host_port
                environment=container_env,
                extra_hosts=extra_hosts
            )
            yield f"Container '{container.name}' started with ID: {container.short_id}\n"
            yield f"Access the {comp_name} at http://localhost:{host_port}\n"

        yield "\n--- DEPLOYMENT COMPLETE ---\n"

    except Exception as e:
        yield f"\n--- \nError during deployment: {str(e)}\n"
    finally:
        # --- 5. Cleanup ---
        if clone_dir and os.path.exists(clone_dir):
            yield "Cleaning up cloned repository...\n"
            shutil.rmtree(clone_dir)


# --- FastAPI Application ---
app = FastAPI()

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET:
        return True
    if not signature_header:
        return False
    
    try:
        sha_name, signature = signature_header.split("=")
        if sha_name != "sha256":
            return False
        
        mac = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), msg=payload_body, digestmod=hashlib.sha256)
        return hmac.compare_digest(mac.hexdigest(), signature)
    except Exception:
        return False

async def run_deployment_in_background(pr_url: str):
    try:
        owner, repo_name, pr_number = parse_pr_url(pr_url)
        log_filename = f"deployment_pr_{pr_number}.log"
    except Exception:
        log_filename = "deployment_unknown.log"
        
    os.makedirs("logs", exist_ok=True)
    log_path = os.path.join("logs", log_filename)
    
    print(f"Starting background deployment for {pr_url}. Logs will be written to {log_path}")
    
    try:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n=== Deployment started at {datetime.now().isoformat()} ===\n")
            async for log_line in deploy_pr_logic(pr_url):
                sys.stdout.write(log_line)
                sys.stdout.flush()
                log_file.write(log_line)
                log_file.flush()
    except Exception as e:
        error_msg = f"Error in background deployment wrapper: {e}\n"
        sys.stderr.write(error_msg)
        sys.stderr.flush()

@app.post("/api/deploy")
async def deploy(request: Request):
    """
    Handles the deployment request and streams logs back to the client.
    """
    data = await request.json()
    pr_url = data.get('pr_url')

    if not pr_url:
        return {"error": "pr_url is required"}, 400

    return StreamingResponse(deploy_pr_logic(pr_url), media_type='text/plain')

@app.post("/api/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    payload_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")
    
    if GITHUB_WEBHOOK_SECRET and not verify_signature(payload_body, signature_header):
        return {"error": "Invalid signature"}, 401
        
    event_type = request.headers.get("X-GitHub-Event", "ping")
    
    if event_type == "ping":
        return {"message": "pong"}
        
    try:
        payload = await request.json()
    except Exception:
        return {"error": "Invalid JSON"}, 400
        
    if event_type == "pull_request":
        action = payload.get("action")
        pr = payload.get("pull_request", {})
        merged = pr.get("merged", False)
        pr_url = pr.get("html_url")
        
        if action == "closed" and merged:
            if pr_url:
                background_tasks.add_task(run_deployment_in_background, pr_url)
                return {"message": f"Deployment triggered for merged PR: {pr_url}"}
            else:
                return {"error": "PR URL not found in payload"}, 400
        else:
            return {"message": f"PR action '{action}' (merged={merged}) ignored."}
            
    return {"message": f"Event '{event_type}' ignored."}

# Frontend React app mounting removed/disabled as requested.

if __name__ == "__main__":
    # Use uvicorn to run the app
    uvicorn.run(app, host='0.0.0.0', port=5001)
