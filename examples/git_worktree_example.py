import asyncio
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from cloud_autopkg_runner import (
    GitClient,
    Recipe,
    RecipeFinder,
    Settings,
    shell,
    logging_config,
)


@asynccontextmanager
async def worktree(
    client: GitClient, path: Path, branch: str
) -> AsyncGenerator[GitClient]:
    """Create a git worktree for a branch, then remove it on exit."""
    await client.branch(branch_name=branch)
    await client.add_worktree(path=path, branch_or_commit=branch)
    try:
        yield GitClient(path)
    finally:
        await client.prune_worktrees()
        await client.remove_worktree(path=path, force=True)


async def create_pull_request(branch: str, recipe: str) -> None:
    """Create a GitHub PR for the given branch using GitHub CLI."""
    logger = logging_config.get_logger(__name__)
    repo = os.environ["GITHUB_REPOSITORY"]  # owner/repo
    cmd = [
        "gh",
        "pr",
        "create",
        f"--repo={repo}",
        f"--head={branch}",
        f"--title=AutoPkg update: {recipe}",
        f"--body=Automated update for recipe `{recipe}`.",
    ]

    try:
        _returncode, stdout, _stderr = await shell.run_cmd(cmd, capture_output=True)
        logger.info("Opened PR for %s: %s", recipe, stdout.strip())
    except Exception as e:
        logger.error("Failed to create PR for %s: %s", recipe, e)


async def process_recipe(recipe: Path, git_repo_root: Path) -> None:
    """Run a recipe in its own branch and submit a PR if changes exist."""
    recipe_name = recipe.stem
    now = datetime.now(timezone.utc)
    branch = f"autopkg/{recipe_name}-{now:%Y%m%d%H%M%S}"
    worktree_path = git_repo_root.parent / f"worktree-{recipe_name}-{now:%Y%m%d%H%M%S}"
    logger = logging_config.get_logger(__name__)

    logger.info("Processing %s", recipe_name)
    async with worktree(GitClient(git_repo_root), worktree_path, branch) as client:
        try:
            await Recipe(recipe).run()
            logger.info("Recipe %s complete", recipe_name)
        except Exception as e:
            logger.error("Recipe %s failed: %s", recipe_name, e)
            return

        status = await client.status(porcelain=True)
        if not status.strip():
            logger.info("No changes to commit for %s", recipe_name)
            return

        await client.add("Munki/")
        await client.commit(
            message=f"AutoPkg {recipe_name} {now.isoformat(timespec='seconds')}",
            all_changes=True,
        )
        await client.push(branch=branch, set_upstream=True)
        logger.info("Pushed branch %s", branch)

        await create_pull_request(branch, recipe_name)


async def main() -> None:
    autopkg_dir = Path("AutoPkg")
    git_repo_root = Path()

    settings = Settings()
    settings.cache_plugin = "json"
    settings.cache_file = "metadata_cache.json"
    settings.log_file = Path("autopkg_runner.log")
    settings.report_dir = autopkg_dir / "Reports"
    settings.verbosity_level = 3

    logging_config.initialize_logger(settings.verbosity_level, str(settings.log_file))

    recipe_finder = RecipeFinder()
    recipe_list = json.loads((autopkg_dir / "recipe_list.json").read_text())
    recipe_paths = [await recipe_finder.find_recipe(r) for r in recipe_list]
    # recipe_paths: list[Path] = []
    # for r in recipe_list:
    #     path = await recipe_finder.find_recipe(r)
    #     print("Resolved recipe %s -> %s", r, path)
    #     recipe_paths.append(path)

    await asyncio.gather(
        *(process_recipe(recipe, git_repo_root) for recipe in recipe_paths)
    )


if __name__ == "__main__":
    asyncio.run(main())
