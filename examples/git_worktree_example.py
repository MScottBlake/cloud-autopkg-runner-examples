import asyncio
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from cloud_autopkg_runner import (
    AutoPkgPrefs,
    GitClient,
    Recipe,
    RecipeFinder,
    Settings,
    logging_config,
    shell,
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


async def process_recipe(
    recipe: Path, git_repo_root: Path, autopkg_prefs: AutoPkgPrefs
) -> None:
    """Run a recipe in its own branch and submit a PR if changes exist."""
    settings = Settings()
    logger = logging_config.get_logger(__name__)
    base_git_client = GitClient(git_repo_root)

    recipe_name = recipe.stem
    now = datetime.now(timezone.utc)
    branch = f"autopkg/{recipe_name}-{now:%Y%m%d%H%M%S}"
    worktree_path = git_repo_root.parent / f"worktree-{recipe_name}-{now:%Y%m%d%H%M%S}"

    logger.info("Processing %s", recipe_name)
    async with worktree(base_git_client, worktree_path, branch) as client:
        autopkg_prefs.munki_repo = worktree_path / "Munki"

        try:
            results = await Recipe(recipe, settings.report_dir, autopkg_prefs).run()
            logger.debug("AutoPkg recipe run results: %s", results)
            logger.info("Recipe run %s complete", recipe_name)
        except Exception as e:
            logger.error("Recipe %s failed: %s", recipe_name, e)
            return

        if not results["munki_imported_items"]:
            logger.info("No changes to commit for %s", recipe_name)
            return

        munki_repo_path = str(autopkg_prefs.munki_repo)
        for item in results["munki_imported_items"]:
            await client.add(
                [
                    f"{munki_repo_path}/icons/{item.get('icon_repo_path')}",
                    f"{munki_repo_path}/pkgsinfo/{item.get('pkginfo_path')}",
                    # f"{munki_repo_path}/pkgs/{item.get('pkginfo_path')}",  # Gitignored
                ]
            )

        await client.commit(
            message=f"AutoPkg {recipe_name} {now.isoformat(timespec='seconds')}"
        )
        await client.push(branch=branch, remote="origin", set_upstream=True)
        logger.info("Pushed branch %s", branch)

        await create_pull_request(branch, recipe_name)


async def main() -> None:
    autopkg_prefs = AutoPkgPrefs()
    autopkg_dir = Path("AutoPkg")
    git_repo_root = Path(os.environ.get("GITHUB_WORKSPACE", "."))

    settings = Settings()
    settings.cache_plugin = "json"
    settings.cache_file = "metadata_cache.json"
    settings.log_file = Path("autopkg_runner.log")
    settings.report_dir = autopkg_dir / "Reports"
    settings.verbosity_level = 3

    logging_config.initialize_logger(settings.verbosity_level, str(settings.log_file))

    recipe_finder = RecipeFinder(autopkg_prefs)
    recipe_list = json.loads((autopkg_dir / "recipe_list.json").read_text())
    recipe_paths = [await recipe_finder.find_recipe(r) for r in recipe_list]

    await asyncio.gather(
        *(
            process_recipe(recipe, git_repo_root, autopkg_prefs.clone())
            for recipe in recipe_paths
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
