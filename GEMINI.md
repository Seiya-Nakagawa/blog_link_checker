# GEMINI.md

このファイルは、本リポジトリ固有の事情を記録したものです。

基本方針・機密情報の取り扱い・Git 運用・コーディング規約・Markdown 記法などの共通規約は
グローバル規約（`~/.claude/CLAUDE.md`、`~/.gemini/GEMINI.md`、`~/.claude/rules/`）に従います。
**本ファイルに共通規約を重複定義しないこと。**

## Project overview

Blog Link Checker: a scheduled system that crawls blog articles (Hatena Blog / Livedoor Blog), extracts affiliate/ad links following a specific marker string, and checks whether those links are still alive. Three components hand off to each other asynchronously via S3:

1. **`gas/`** (Google Apps Script, triggered daily) — reads target URLs from a Google Sheet, writes `urls_list.json` to S3.
2. **`terraform/lambda/link_checker_lambda.py`** (Python, AWS Lambda) — triggered by the S3 `*urls_list.json` PUT event, crawls the URLs, checks each extracted link, writes `linkcheck_result.csv` back to S3.
3. **`gas/`** (second daily trigger, timed to run after Lambda finishes) — downloads the CSV, writes it into the "today" sheet, diffs against the "yesterday" sheet, highlights changes, emails a summary, and rotates backup sheets.

Full spec: `docs/【ブログリンクチェッカー】要件定義書.md` (requirements) and `docs/【ブログリンクチェッカー】基本設計書.md` (design — includes the S3 JSON/CSV schemas). Read the 基本設計書 before changing any cross-component data contract (the JSON keys `auto_url_list`/`manual_url_list`, or the CSV headers).

## Repository layout

```
gas/          Google Apps Script source (manually pasted into the GAS project — see Deployment notes)
terraform/    Terraform (Terraform Cloud backend) + Lambda source
docs/         要件定義書 / 基本設計書 / 構成図 — the source of truth for behavior, read before changing logic
```

There is no test suite, linter, or build tooling in this repo (no `package.json`, no Python test config). Treat the docs and existing code behavior as the spec.

## Commands

### Terraform (infra + Lambda deploy)

Deploys run in **Terraform Cloud**, triggered when a change lands on `main` (i.e. when a PR is merged — see the global `git-workflow.md`); `apply` does not run locally.

```bash
cd terraform
terraform init
terraform login   # first time only
terraform plan     # local plan check only; apply happens in Terraform Cloud on push
```

Lambda deployment is bundled into `terraform apply`: `data.tf` zips `terraform/lambda/link_checker_lambda.py` via `archive_file` on every plan/apply, so editing the Lambda code just requires a normal `git push` — no manual packaging step. The Python dependency layer (`requirements.txt` contents: `requests`, `beautifulsoup4`) is a separately-managed zip pre-uploaded to `s3://<bucket>/lambda-layers/${system_name}_python_libraries.zip`; Terraform only reads it via `data.aws_s3_object`, it does not build it.

### GAS

A clasp project is configured at the repo root (`.clasp.json`, `rootDir: "gas"`), bound (via `parentId`) to the existing 作業用スプレッドシート — so the global `~/.claude/rules/gas-deploy-flow.md` clasp flow (`clasp push` → `clasp deployments` → `clasp deploy -i`) applies here. Auth is project-scoped, not the machine-global clasp login: set `clasp_config_auth=$(pwd)/.clasp-auth.json` (or pass `--auth .clasp-auth.json`) before running any `clasp` command, so this project's login doesn't clobber `~/.clasprc.json` used by unrelated GAS projects on the same machine. `.clasp-auth.json` contains OAuth tokens and must never be committed (already gitignored).

Script Properties actually read by the code (`SPREADSHEET_ID_WORK`, `SPREADSHEET_ID_SOURCE`, `S3_BUCKET_NAME`, `EMAIL_ADDRESSES`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) must be set in the GAS project UI (Project Settings → Script Properties); there's no local `.env` for them, and clasp has no API for setting them remotely. Note `S3_BUCKET_REGION` is declared in `config.gs`'s `getScriptConfiguration_()` and read by `post_result_s3download.gs`, but is *not* set in the production Script Properties and the system runs fine without it — the S3 library apparently tolerates an undefined region. Don't assume it needs a value unless something actually breaks.

The GAS project owner account and the 原本スプレッドシート were migrated in 2026-07 to `aibdlnew1.work@gmail.com` / a new source spreadsheet file; the 作業用スプレッドシート was kept as-is (the new account was added as an editor rather than moving that file).

## Architecture notes / gotchas

- **Config duplication across GAS files — not actually identical.** `gas/config.gs` defines the canonical `getScriptConfiguration_()` (7 keys, including `S3_BUCKET_REGION`) and a module-level `CONFIG` constant. `gas/pre_url_s3upload.gs` independently defines its *own* `getScriptConfiguration_()` and never references the global `CONFIG` — it always calls its local function, which is missing `S3_BUCKET_REGION` (only 6 keys). This works today only because `pre_url_s3upload.gs` never reads that key. `gas/post_result_s3download.gs`, by contrast, relies entirely on the global `CONFIG` from `config.gs` and defines no config function of its own. Because GAS concatenates all `.gs` files into one global namespace, having two functions named `getScriptConfiguration_` is fragile — if you edit one, edit the other, or better, delete the duplicate in `pre_url_s3upload.gs` and make it use the shared `CONFIG`.
- **Email variables**: `terraform/variables.tf` has both `notification_emails` (common) and `notification_emails_blog` (this project only); `terraform/sns.tf` subscribes the de-duplicated union of both to the SNS topic. GAS-side notifications use the separate `EMAIL_ADDRESSES` script property — the two notification paths (SNS/CloudWatch alarms vs. GAS `MailApp`) are independent and not kept in sync automatically.
- **S3 as the sole integration point** between GAS and Lambda — there is no direct API call in either direction. Upload triggers Lambda via `aws_s3_bucket_notification` (suffix filter `urls_list.json`); completion is signaled back to GAS by a separate flag file (`lambda_completion_status.json`, written by `write_completion_status()` in `link_checker_lambda.py` and read by `checkLambdaCompletionStatus_()` in `post_result_s3download.gs`) rather than by the CSV's presence alone. `write_completion_status()` is called both on success (`{"status": "SUCCESS", "last_success_date": "yyyy-MM-dd"}`) and on failure (`{"status": "FAILURE", "error_message": ...}`) inside `lambda_handler`'s try/except, so the flag always reflects the most recent invocation's outcome. GAS's `mainPostProcess` only proceeds if `status === 'SUCCESS'` and `last_success_date` matches today (Asia/Tokyo); it deletes the flag file (`deleteS3FlagFile_()`) once post-processing completes, so a stale flag from a prior day should never cause a false positive. (Before 2026-07-07 the Lambda never wrote this file at all, so GAS silently aborted post-processing every run without raising an error — check `git blame` on this bullet if similar "completed but did nothing" behavior reappears.)
- **Diff logic** in `compareAndHighlightDifferences_()` keys rows on `記事URL|リンク先URL` (columns B/C, i.e. indices 1/2) and ignores the timestamp column (index 7) when detecting changes — extend this key logic carefully if the CSV schema (`CSV_HEADERS` in the Lambda / column layout in `config.gs`'s `RESULT_SHEET_COLUMN_COUNT = 8`) ever changes; the two are not derived from a shared schema definition, so update both together.
- **Ad-link extraction is marker-text-based**, not selector-based: the Lambda searches rendered HTML for the literal string `「※一部、広告・宣伝が含まれます。」` and takes the first `<a>` after it. Platform-specific pagination (`rel='next'` for Hatena, `a.next`/"次へ" for Livedoor) is handled by separate functions (`find_hatena_next_page_link` / `find_livedoor_next_page_link`) — new blog platforms need both a pagination finder and to be added to the `is_hatena`/`is_livedoor` URL-sniffing branch in `lambda_handler`.
- All Lambda tuning (timeouts, retries, worker count, NG words, exclude strings) is environment-variable-driven from Terraform (`terraform/lambda.tf` → `terraform/variables.tf`), not hardcoded — check `variables.tf` before assuming a constant is fixed.
