# Multi-Agent-writer-system-

## How to Run

1. Create the virtual environment:
   ```bash
   python -m venv venv
   ```

2. Activate the virtual environment:
   **Windows (PowerShell):**
   ```powershell
   venv\Scripts\Activate.ps1
   ```

   **Windows (Command Prompt):**
   ```cmd
   venv\Scripts\activate.bat
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the application:
   - For FastAPI server:
     ```bash
     uvicorn main:app --reload
     ```
   - For Streamlit UI:
     ```bash
     streamlit run ui.py
     ```

## System Architecture

This multi-agent writer system consists of:

### Core Components
- **Multi-Agent Framework**: CrewAI agents for different writing tasks
- **LLM Providers**: OpenAI and Anthropic for language model integration
- **Web Framework**: FastAPI for REST API and Streamlit for web UI
- **Utilities**: Data processing, configuration, and logging tools

### Available Agents
- Content Generator Agent
- Research Agent  
- Editor Agent
- Formatting Agent

### Key Features
- Multi-agent collaboration
- Natural language understanding
- Automated report generation
- Template-based formatting
- Real-time preview

## Project Structure
```
Multi-Agent-writer-system-
├── venv/                    # Virtual environment
├── requirements.txt         # Dependencies
├── main.py                  # Core application
├── ui.py                    # Streamlit interface
├── agents/                  # Agent implementations
├── templates/              # Report templates
└── config/                 # Configuration files
```

## Usage Examples

### Command Line Interface
```bash
# Generate a new report
python main.py generate --topic "AI Trends" --format pdf

# Research and write
python main.py research --query "machine learning ethics"

# Format existing content
python main.py format --input draft.txt --output formatted.md
```

### Web Interface
Access the web UI at: `http://localhost:8501`

## Dependencies

The system requires Python 3.8+ and includes the following key dependencies:
- **crewai** - Multi-agent orchestration
- **openai** - OpenAI API integration  
- **anthropic** - Anthropic API integration
- **fastapi** - Web framework
- **streamlit** - Web UI
- **pandas** - Data processing

## Development

### Running Tests
```bash
pytest
```

### Code Quality
```bash
black .
flake8 .
mypy .
```

## License
This project is licensed under the terms of the MIT License.

postgresql://ad69134:ZgahxyWlEvWchIViTEMe7qXntHsR12NX@dpg-dc2q86jm8hqs73894u5g-a.oregon-postgres.render.com/writermemory