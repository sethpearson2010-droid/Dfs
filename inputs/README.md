# inputs/salary.csv

This is the file the GitHub Actions workflow reads — replace it each
week with the current FanDuel slate export.

## Replacing it from a phone

1. On FanDuel's lineup builder page, tap "Download CSV" (you may need
   "Request desktop site" in your mobile browser for FanDuel's export
   button to appear).
2. In GitHub (github.com, in your phone's browser — desktop-site mode
   makes the editor UI easier to use), navigate to this file:
   `inputs/salary.csv`.
3. Tap the pencil/edit icon, select all the existing content, delete
   it, and paste in the new CSV content. Commit directly to `main`.
   (GitHub's mobile web editor is a plain text box, so pasting CSV
   text works even though it's not a "real" upload button.)
4. Go to the **Actions** tab → **Run pipeline and deploy dashboard** →
   **Run workflow**, fill in the season/risk level, and run it.
5. Once it finishes (a couple minutes), open your Pages URL
   (**Settings → Pages** shows it, something like
   `https://<username>.github.io/<repo>/`) to see the dashboard.

The placeholder data currently in this file is a small real-2025-data
test slate (`test_data/full_slate.csv`) — safe to leave in place while
you're just testing the setup, but replace it before relying on the
output for an actual slate.
