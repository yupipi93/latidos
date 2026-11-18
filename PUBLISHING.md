# Publishing

This repository is private. Its history holds data from real use and is never made
public as it is. A public release is a fresh snapshot of a commit that passes every
check below, and creating the public repository needs the owner's explicit go.

## Checklist

1. **Readiness scan READY** on the exact commit to publish: no denylisted term, absolute
   home path, tailnet host or real email in tracked files, and `README.md`, `LICENSE`,
   `AGENTS.md`, `PUBLISHING.md` and `.gitignore` present. The scan is the owner's
   `public-readiness.py <clone>` from the private workspace.
2. **Suite green**: `python3 -m unittest -q test_latidos` and
   `node --test web/format.test.js`, and the CI workflow green on that commit.
3. **Fresh orphan snapshot**, never this history:

   ```bash
   git checkout --orphan public
   git commit -m "feat: initial public release"   # author: your name and a GitHub noreply address
   ```

   The author is a name plus a GitHub noreply address
   (`git -c user.name="<Name>" -c user.email="<id>+<user>@users.noreply.github.com" commit ...`).
   Publish the snapshot only into a brand-new public repository. Never make this private
   repository public and never force-push the snapshot into it: after a force-push and a
   visibility change GitHub still serves the old pull requests with their diffs, and the
   old commits by hash. To keep the name, rename this repository first (for example
   `<name>-private`), then create the new public one under the original name.
   Never push the private branches or tags.
4. **Leak guard on the public clone**: install the workspace's
   `migration/pre-push-leak-guard.sh` as its `pre-push` hook with `LEAK_GUARD_ALL=1`.
5. **Review by eye**: screenshots, demo data, `config/*.example.json` and every fixture
   use synthetic values only (`DEMO-123`, invented people, example.com). Screenshots
   come from a synthetic database, never from real history.
6. **Owner's go**: the public repository is created only on the owner's explicit go for
   that repository.
