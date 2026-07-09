import os
import re
import shutil
import sys
from urllib.parse import urlparse

import uvicorn
import docker
from github import Github
import git
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

# Load environment variables from .env file
load_dotenv()


# --- Configuration ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
CLONE_DIR = "temp_repo"

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

    try:
        # --- 1. Parse URL and Fetch PR Info ---
        owner, repo_name, pr_number = parse_pr_url(pr_url)
        yield f"Deploying PR #{pr_number} from {owner}/{repo_name}...\n"

        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(f"{owner}/{repo_name}")
        pr = repo.get_pull(pr_number)

        head_branch = pr.head.ref
        head_repo_url = pr.head.repo.clone_url
        
        yield f"Source branch: '{head_branch}' from repository '{pr.head.repo.full_name}'\n"

        # --- 2. Clone Repository and Checkout PR Branch ---
        if os.path.exists(CLONE_DIR):
            yield f"Removing existing directory: {CLONE_DIR}\n"
            shutil.rmtree(CLONE_DIR)

        yield f"Cloning repository from {head_repo_url}...\n"
        cloned_repo = git.Repo.clone_from(head_repo_url, CLONE_DIR, branch=head_branch)
        yield f"Successfully cloned and checked out branch '{head_branch}'.\n"

        # --- 3. Build Docker Image ---
        image_tag = f"{repo_name.lower()}-pr-{pr_number}"
        yield f"Building Docker image with tag: {image_tag}\n"

        # Define the path to the Dockerfile relative to the clone directory
        dockerfile_path = os.path.join(CLONE_DIR, "backend/Dockerfile")
        # The build context is the root of the repository, so the Dockerfile can access both /frontend and /backend
        build_context_path = CLONE_DIR

        docker_client = docker.from_env()
        if not os.path.exists(dockerfile_path):
             yield f"Error: Dockerfile not found at '{dockerfile_path}'.\n"
             return

        image, build_logs = docker_client.images.build(
            path=build_context_path,
            dockerfile="backend/Dockerfile", # Specify the Dockerfile location
            tag=image_tag,
            rm=True
        )
        for chunk in build_logs:
            if 'stream' in chunk:
                for line in chunk['stream'].splitlines():
                    yield line + '\n'
        yield f"Successfully built image: {image.short_id}\n"

        # --- 4. Run Docker Container ---
        container_name = f"{repo_name}-pr-{pr_number}-container"
        
        try:
            # Stop and remove container if it already exists
            existing_container = docker_client.containers.get(container_name)
            yield f"Stopping and removing existing container: {container_name}\n"
            existing_container.stop()
            existing_container.remove()
        except docker.errors.NotFound:
            pass # Container doesn't exist, which is fine

        yield f"Running Docker container '{container_name}'...\n"
        container = docker_client.containers.run(
            image_tag,
            detach=True,
            name=container_name,
            ports={'8000/tcp': 8080} # Maps port 8000 (from Dockerfile) to 8080 on host
        )
        yield f"Container '{container.name}' started with ID: {container.short_id}\n"
        yield "Access the application at http://localhost:8080 (port may vary).\n"
        yield "\n--- DEPLOYMENT COMPLETE ---\n"

    except Exception as e:
        yield f"\n--- \nError during deployment: {str(e)}\n"
    finally:
        # --- 5. Cleanup ---
        if os.path.exists(CLONE_DIR):
            yield "Cleaning up cloned repository...\n"
            shutil.rmtree(CLONE_DIR)


# --- FastAPI Application ---
app = FastAPI()

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

# Mount the static files directory to serve the React app
# This must be after all other API routes
app.mount("/", StaticFiles(directory="../frontend/build", html=True), name="static")

if __name__ == "__main__":
    # Use uvicorn to run the app
    uvicorn.run(app, host='0.0.0.0', port=5001)
