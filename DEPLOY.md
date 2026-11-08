# Your checklist (about 30 minutes)

Everything else is done. These steps need your accounts or an internet connection.

## 1. Run the LLM version (15 min, mostly waiting)
1. Unzip `kuku_promo_finder_code.zip`, then copy the 50 episode `.txt` files into `kuku/data/episodes/`.
2. Get a free key at https://aistudio.google.com/apikey.
3. In a terminal:
   ```bash
   cd kuku
   export GEMINI_API_KEY=paste_key_here
   bash run_all.sh
   ```
   If it says the model isn't found, open `config.yaml`, set `model:` to a current Flash model listed in AI Studio, and run it again.
4. Send Claude `outputs/evaluation.txt` and `outputs/llm/report.md`. Claude fills in the doc's numbers and the LLM failure examples.

## 2. Put the code on GitHub (5 min)
1. Go to https://github.com/new, name the repo `promo-finder`, choose **Private**, and click Create.
2. In the `kuku` folder:
   ```bash
   git init && git add . && git commit -m "Promo Finder"
   git branch -M main
   git remote add origin https://github.com/<you>/promo-finder.git
   git push -u origin main
   ```
   `data/` and `outputs/` are git-ignored, so Kuku's scripts never leave your machine this way.

## 3. Deploy the prototype (5 min)
1. Go to https://share.streamlit.io, sign in with GitHub, and click **Create app**, then choose **Deploy a public app from GitHub**.
2. Set the repo to `promo-finder`, the branch to `main`, and the main file to `app.py`. Click **Deploy**.
3. When it loads, test it: upload a zip of 3 episodes, choose **Rules only**, and click **Find promos**.
4. Don't add your API key as a secret. Reviewers can use Rules only, or paste their own key in LLM mode.
5. Copy the app URL (`https://....streamlit.app`).

Private repo: Streamlit asks for permission to read it, so allow that. The app itself is public, which the brief requires, but it contains no screenplays.

## 4. Share the raw outputs (2 min)
Upload `raw_outputs.zip` to Google Drive, then set Share to **Anyone with the link → Viewer**. Copy the link.

## 5. Finish the doc
Paste the three links (app, GitHub, Drive) into the doc's **Links** section. Export it as a PDF and submit the form.
For GitHub: either make the repo public (it holds no screenplays) or add the reviewers as collaborators.
