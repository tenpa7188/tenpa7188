# プロフィールREADME運用メモ

この README は `scripts/update_readme.py` を GitHub Actions から実行し、`Latest Projects` と `Latest Articles` のみを自動更新する構成です。  
固定文はマーカーの外側に置いているため、自動更新時にも自己紹介や注力領域は保持されます。

## 使い方

1. `profile_readme_config.json` の `github_username`、`zenn_username`、`profile_repository_name` を自分の値に変更します。
2. このリポジトリを GitHub のプロフィール用リポジトリとして公開します。
3. GitHub Actions を有効化し、`Update GitHub Profile README` ワークフローを手動実行または定期実行します。
4. 新しい公開リポジトリや Zenn 記事が追加されると、README の対象セクションだけが更新されます。

## 確認ポイント

README 更新が push で失敗する場合は、GitHub の `Settings > Actions > General > Workflow permissions` が `Read and write permissions` になっているか確認してください。
