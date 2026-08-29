name: economic-data

on:
  schedule:
    # Sunday 12:00 UTC = 16:00 Dubai — week-ahead economic calendar
    - cron: "0 12 * * 0"
  workflow_dispatch: {}

jobs:
  post:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          pip install pillow
          sudo apt-get install -y fonts-dejavu-core
          mkdir -p fonts
          curl -sL -o fonts/IBMPlexSans.ttf "https://raw.githubusercontent.com/google/fonts/main/ofl/ibmplexsans/IBMPlexSans%5Bwdth%2Cwght%5D.ttf"
      - name: Post week-ahead economic calendar
        env:
          DISCORD_WEBHOOK_ECONOMIC_DATA: ${{ secrets.DISCORD_WEBHOOK_ECONOMIC_DATA }}
        run: python scripts/economic_data.py
