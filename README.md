<p align="center">
  <img src="logo.png" alt="Logo" width="300"/>
</p>

# HateHunter: Detect Hate Speech on YouTube with AI

HateHunter is a command-line Python tool that scans YouTube content for hate speech using OpenAI's Moderation API. Effortlessly identify and analyze harmful content with AI-driven moderation.

🔍 Key Features:

* Detect hate speech in YouTube videos
* Leverage OpenAI’s advanced Moderation API
* Optionally highlight user-specified keywords
* Manage different investigations in projects, new results will be merged.
* Perfect for researchers, moderators, and developers seeking automated content analysis. 🚀

#YouTube #AI #HateSpeechDetection #ContentModeration #Python #OpenAI

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/lobuhi/HateHunter.git
   cd HateHunter
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Ensure yt-dlp is installed:
   ```bash
   pip install yt-dlp
   ```
   (Alternatively, follow the [yt-dlp GitHub](https://github.com/yt-dlp/yt-dlp) instructions.)

## Usage

You can run the tool in two modes: processing one or more individual videos or an entire channel. Results are merged into project files (JSON and HTML).
If a video has already been processed (its ID is found in the project file), the tool will exit without reprocessing it.

## Command-Line Options

Command‑Line Options
The tool provides a number of command‑line options to customize its behavior. Here’s an overview of each option:

`--channel <URL>`
Process an entire YouTube channel by providing its URL. This option is mutually exclusive with --video.

`--video <URL> [URL ...]`
Process one or more individual YouTube videos by specifying their URLs. Multiple URLs can be provided (separated by spaces). This option is mutually exclusive with --channel.

`--language <lang>`
Specify the language code for subtitles. For example, use es for Spanish or en for English. Defaults to en if not specified.

`--openai-api-key <KEY>`
Provide your OpenAI API key. The tool will first check if the environment variable OPENAI_API_KEY is set and use that. If not, it will use the value provided here. If neither is provided, the tool will prompt you to enter your API key.

`--threshold <seconds>`
Set the time threshold (in seconds) used for grouping subtitle blocks during conversion. The default is 30 seconds.

`--skip-convert`
If this flag is set, the tool will skip converting the downloaded SRT files into the simplified .s30 format.

`--skip-analyze`
If this flag is set, the tool will skip the analysis step that uses the OpenAI Moderation API, so it just will download the subtitles.

`--keywords or -k "<keyword1, keyword2, ...>"`
Provide a comma-separated list of keywords. The tool will filter and highlight lines containing any of these keywords in the final HTML report. For example:
`-k "jew, muslim, black, immigration"`
If omitted, all lines are processed without keyword-based filtering or highlighting.

`--project or -p <project_name>`
Define the project name to be used as the base for the output JSON and HTML files (e.g., `hatespeech` will produce `hatespeech.json` and `hatespeech.html`).

* **First Run**: A new project file is created.
* **Subsequent Runs**: The tool will check if a video has already been processed (based on its video ID) and exit if duplicates are detected; otherwise, it appends new, unique results to the existing files.
These options allow you to tailor the tool's behavior to your needs, whether you’re processing individual videos, an entire channel, or merging analyses over time.

### Example 1: Process a single video
```bash
python3 hatehunter.py --openai-api-key <YOUR_API_KEY> --language es --video https://www.youtube.com/watch?v=VIDEO_ID -k "term1, term2, term3" -p projectname
```

### Example 2: Process multiple videos channel
```bash
python3 hatehunter.py --openai-api-key <YOUR_API_KEY> --language es --video https://www.youtube.com/watch?v=VIDEO_ID --video https://www.youtube.com/watch?v=VIDEO_ID2 --video https://www.youtube.com/watch?v=VIDEO_ID3 -k "term1, term2, term3" -p projectname
```

### Example 3: Process an entire channel
```bash
export OPENAI_API_KEY=<YOUR API KEY>
python3 hatehunter.py --language en --channel https://www.youtube.com/c/ChannelName -p projectname
```
## Requirements

* Python 3.6+
* yt-dlp
* Python packages: openai, requests
* OpenAI API Key

## Contributing

Contributions, issues, and feature requests are welcome! Feel free to open an issue or submit a pull request. Or **[buy me a coffee.](https://buymeacoffee.com/lobuhi)**
