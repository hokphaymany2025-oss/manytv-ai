# Commit Changes

Prepare a clean git commit.

Tasks:

1. Run:

git status
git diff

2. Check:

- no unrelated files
- no temporary files
- no secrets
- tests passed

3. Stage only required files.

Create commit:

git add <files>

git commit -m "<clear message>"

After commit:

git status
git log --oneline -5

Do not push unless requested.
