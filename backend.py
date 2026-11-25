"""
Backend module for the Multi-Agent Report Writer System.
Four agents (Researcher, Writer, Reviewer, Final Editor) orchestrated with LangGraph,
Gemini as the LLM, PostgreSQL state checkpointer and Redis cache.

Workflow:
    researcher_node -> writer_node -> reviewer_node --(revise)--> writer_node
                                                    \--(done)---> final_node -> END
"""
import time
import re
import os
import logging
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import TypedDict, Dict, Any, List
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from crewai import Agent, Task, Crew, LLM
from tools.tavily_search import TavilySearchTool
import psycopg2
import psycopg2.extras
import redis

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

WORD_TARGETS = {"short": 300, "detailed": 450, "comprehensive": 700}

def short(value, n=1500):
    """Cut long text so each request stays small."""
    return str(value)[:n]

def fixes_only(review: str, n=600) -> str:
    """Keep only the FIXES part of the reviewer output, not the whole review."""
    parts = re.split(r"FIXES:", str(review), flags=re.I)
    return short(parts[-1].strip() if len(parts) > 1 else review, n)

def pause():
    """Small delay between LLM calls to stay inside free-tier rate limits."""
    time.sleep(float(os.environ.get("AGENT_STEP_DELAY", 10)))

def target_words(state) -> int:
    """Hard word target so the writer never produces a 30-page report."""
    level = str(state.get("user_settings", {}).get("report_length", "detailed")).lower()
    return WORD_TARGETS.get(level, 450)

def search_arxiv(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Real paper search (arXiv, no API key). Only these papers may be cited."""
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": f"all:{query}", "start": 0, "max_results": max_results}
    )
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        logger.warning(f"arXiv search failed: {e}")
        return []
    papers = []
    for entry in root.findall("a:entry", ns):
        papers.append({
            "title": " ".join(entry.findtext("a:title", "", ns).split()),
            "authors": [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)],
            "year": (entry.findtext("a:published", "", ns) or "")[:4],
            "url": entry.findtext("a:id", "", ns),
            "abstract": " ".join(entry.findtext("a:summary", "", ns).split()),
        })
    return papers

def format_papers(papers, limit=2, abs_chars=250):
    """Compact numbered paper list: at most 2 papers, short abstracts."""
    if not papers:
        return "No papers. Do not cite any."
    return "\n".join(
        f"[{i}] {p['authors'][0] if p['authors'] else 'Unknown'} et al. ({p['year']}). "
        f"{p['title']}. Abstract: {p['abstract'][:abs_chars]}"
        for i, p in enumerate(papers[:limit], 1)
    )

def build_references(state, draft: str) -> str:
    """References are built in Python, so the LLM never has to write them."""
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", draft)}
    lines = [
        f"[{i}] {', '.join(p['authors'][:3])} ({p['year']}). {p['title']}. {p['url']}"
        for i, p in enumerate(state.get("papers", [])[:2], 1) if i in cited
    ]
    urls = sorted(set(re.findall(r"https?://[^\s】\)\]]+", state.get("research_notes", ""))))
    out = "\n\n## References\n" + "\n".join(lines)
    if urls:
        out += "\n\n**Web sources**\n" + "\n".join(f"- {u}" for u in urls)
    return out

# State shared by all nodes
class ReportState(TypedDict):
    topic: str
    user_settings: dict
    research_notes: str
    papers: list
    draft: str
    feedback: dict
    final_report: str
    revision_round: int
    node: str
    error: str

# -----------------------------------------------------------------------------
# PostgreSQL Checkpointer with Retry Logic
# -----------------------------------------------------------------------------
class PostgreSQLCheckpointer:
    def __init__(self):
        self.conn = None
        self._initialize_table()

    def _initialize_table(self):
        """Initialize the PostgreSQL table with retry logic."""
        max_retries = 3
        retry_delay = 2  # seconds
        for attempt in range(max_retries):
            try:
                self.conn = psycopg2.connect(
                    os.environ.get("POSTGRES_HOST", "postgres://postgres:postgres@localhost/multi_agent_writer"),
                    cursor_factory=psycopg2.extras.RealDictCursor,
                    connect_timeout=5
                )
                with self.conn.cursor() as cursor:
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS workflow_states (
                            topic TEXT PRIMARY KEY,
                            state JSONB NOT NULL,
                            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                self.conn.commit()
                logger.info("PostgreSQL table initialized successfully.")
                return
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed to connect to PostgreSQL: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Failed to initialize PostgreSQL after retries.")
                    self.conn = None
                    raise

    def save_state(self, topic: str, state: Dict[str, Any]) -> None:
        """Save state to PostgreSQL with retry logic."""
        if not self.conn:
            raise RuntimeError("PostgreSQL connection not established.")

        max_retries = 3
        retry_delay = 2  # seconds
        for attempt in range(max_retries):
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO workflow_states (topic, state) VALUES (%s, %s) "
                        "ON CONFLICT (topic) DO UPDATE SET state = %s, timestamp = CURRENT_TIMESTAMP",
                        (topic, json.dumps(state), json.dumps(state))
                    )
                self.conn.commit()
                return
            except Exception as e:
                self.conn.rollback()
                logger.error(f"Attempt {attempt + 1} failed to save state: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Failed to save state after retries.")
                    raise

    def load_state(self, topic: str) -> Dict[str, Any]:
        """Load state from PostgreSQL with retry logic."""
        if not self.conn:
            raise RuntimeError("PostgreSQL connection not established.")

        max_retries = 3
        retry_delay = 2  # seconds
        for attempt in range(max_retries):
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT state FROM workflow_states WHERE topic = %s",
                        (topic,)
                    )
                    result = cursor.fetchone()
                    if result:
                        state = result['state']
                        return state if isinstance(state, dict) else json.loads(state)
                    return {}
            except Exception as e:
                self.conn.rollback()
                logger.error(f"Attempt {attempt + 1} failed to load state: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Failed to load state after retries.")
                    raise

    def close(self) -> None:
        """Close the PostgreSQL connection."""
        if self.conn:
            self.conn.close()
            logger.info("PostgreSQL connection closed.")

# Mock PostgreSQL Checkpointer for testing or when PostgreSQL fails
class MockPostgreSQLCheckpointer:
    def __init__(self):
        self.states = {}

    def save_state(self, topic: str, state: Dict[str, Any]) -> None:
        self.states[topic] = state

    def load_state(self, topic: str) -> Dict[str, Any]:
        return self.states.get(topic, {})

    def close(self):
        pass

# -----------------------------------------------------------------------------
# Redis Cache Checkpointer
# -----------------------------------------------------------------------------
class RedisCacheCheckpointer:
    def __init__(self):
        self.redis_conn = None
        self._initialize_redis()

    def _initialize_redis(self):
        """Initialize Redis connection with retry logic."""
        max_retries = 3
        retry_delay = 2  # seconds
        for attempt in range(max_retries):
            try:
                self.redis_conn = redis.Redis.from_url(
                    os.environ.get("CACHE_REDIS_URL", "redis://localhost:6379")
                )
                self.redis_conn.ping()
                logger.info("Redis connection initialized successfully.")
                return
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed to connect to Redis: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Failed to initialize Redis after retries.")
                    self.redis_conn = None
                    raise

    def save_state(self, topic: str, state: Dict[str, Any]) -> None:
        """Save state to Redis."""
        if not self.redis_conn:
            raise RuntimeError("Redis connection not established.")
        try:
            self.redis_conn.set(f"{topic}:state", json.dumps(state))
            logger.info(f"State saved to Redis for topic: {topic}")
        except Exception as e:
            logger.error(f"Failed to save state to Redis: {e}")
            raise

    def load_state(self, topic: str) -> Dict[str, Any]:
        """Load state from Redis."""
        if not self.redis_conn:
            raise RuntimeError("Redis connection not established.")
        try:
            state_str = self.redis_conn.get(f"{topic}:state")
            if state_str:
                return json.loads(state_str)
            return {}
        except Exception as e:
            logger.error(f"Failed to load state from Redis: {e}")
            raise

    def close(self) -> None:
        """Close the Redis connection."""
        if self.redis_conn:
            self.redis_conn.close()
            logger.info("Redis connection closed.")

# Mock Redis Checkpointer for testing or when Redis fails
class MockRedisCacheCheckpointer:
    def __init__(self):
        self.states = {}

    def save_state(self, topic: str, state: Dict[str, Any]) -> None:
        self.states[topic] = state

    def load_state(self, topic: str) -> Dict[str, Any]:
        return self.states.get(topic, {})

    def close(self):
        pass

# -----------------------------------------------------------------------------
# LLM and the three agents
# -----------------------------------------------------------------------------
def get_groq_llm(temperature=None, max_tokens=None, model=None):
    kwargs = {
        "model": model or os.environ.get("GROQ_MODEL_NAME", "groq/openai/gpt-oss-20b"),
        "api_key": os.environ.get("GROQ_API_KEY"),
        "max_tokens": max_tokens or int(os.environ.get("MAX_OUTPUT_TOKENS", 2000)),
        "reasoning_effort": "low",   # fewer hidden thinking tokens
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return LLM(**kwargs)

def create_researcher_agent():
    search_tool = TavilySearchTool()
    return Agent(
        role='Senior Research Analyst',
        goal='Break the topic into key questions and find authoritative, current information on each.',
        backstory=(
            "You are an expert researcher who plans a short list of key questions for a topic, "
            "then digs into the web to find authoritative, current and relevant information. "
            "You only report facts you actually found, and you always keep the source."
        ),
        tools=[search_tool],
        
                max_iter=3,
llm=get_groq_llm(max_tokens=1500),
        verbose=True,
        allow_delegation=False
    )

def create_writer_agent():
    return Agent(
        role='Professional Report Writer',
        goal='Transform research notes into a coherent, well-structured, and readable report.',
        backstory=(
            "You are an expert technical writer known for your clarity and attention to detail. "
            "You synthesize research into professional, engaging reports, cite sources accurately, "
            "and never invent facts. When you receive reviewer feedback you fix every issue it lists."
        ),
                
        llm=get_groq_llm(max_tokens=2500),
        verbose=True,
        allow_delegation=False
    )

def create_reviewer_agent():
    return Agent(
        role='Technical Reviewer',
        goal='Evaluate report quality and provide actionable feedback for improvement.',
        backstory=(
            "You are a meticulous technical reviewer with a keen eye for detail. "
            "You assess structure, coverage, factual support, citations and readability, "
            "and give short, specific, constructive feedback."
        ),
                
        llm=get_groq_llm(temperature=0.2, max_tokens=800,
                 model=os.environ.get("groq/openai/gpt-oss-20b")),
        verbose=True,
        allow_delegation=False
    )

def create_final_agent():
    return Agent(
        role='Final Report Editor',
        goal='Combine the research notes, papers, approved draft and reviewer feedback into one polished final report.',
        backstory=(
            "You are a master editor. You take the work of a researcher, a writer and a reviewer and "
            "merge it into a single, consistent, professional document. You apply any remaining "
            "reviewer fixes, keep every fact and citation that is supported, remove anything unsupported, "
            "and finish with an accurate reference list. You never invent facts, sources or numbers."
        ),
                
        llm=get_groq_llm(temperature=0.3, max_tokens=2500),
        verbose=True,
        allow_delegation=False
    )

# Initialize agents
researcher = create_researcher_agent()
writer = create_writer_agent()
reviewer = create_reviewer_agent()
final_agent = create_final_agent()

# -----------------------------------------------------------------------------
# Workflow Nodes
# -----------------------------------------------------------------------------
def researcher_node(state: Dict[str, Any]) -> Dict[str, Any]:
    node_name = "researcher_node"
    try:
        logger.info(f"Starting {node_name}")
        papers = search_arxiv(state["topic"], max_results=3)
        task = Task(
            description=(
                f"Topic: {state['topic']}\n"
                f"Pick at most 3 key questions for a ~{target_words(state)} word report. "
                "Use the Tavily tool ONCE. Return at most 8 bullets, each under 20 words, "
                "grouped by question, each ending with its source URL. "
                "Only facts found in results. No questions or offers."
            ),
            expected_output="Short research notes grouped by key question, with source URLs",
            agent=researcher
        )
        crew = Crew(agents=[researcher], tasks=[task], verbose=True)
        result = crew.kickoff()
        logger.info(f"{node_name} completed successfully")
        pause()
        return {"research_notes": str(result), "papers": papers, "node": node_name}
    except Exception as e:
        logger.error(f"Error in {node_name}: {e}", exc_info=True)
        return {"error": str(e), "node": node_name}

def writer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    node_name = "writer_node"
    try:
        logger.info(f"Starting {node_name}")
        revision_round = state.get("revision_round", 0)

        if revision_round > 0 and state.get("draft"):
            body = (
                f"Revise this draft and fix every issue listed.\n\nDraft:\n{short(state['draft'], 3000)}\n\n"
                f"Fixes:\n{fixes_only(state.get('feedback', {}).get('overall_score', ''))}\n\n"
                f"Papers you may cite as [n]:\n{format_papers(state.get('papers', []))}\n"
            )
        else:
            body = (
                f"Research notes:\n{short(state.get('research_notes'), 2000)}\n\n"
                f"Papers you may cite as [n]:\n{format_papers(state.get('papers', []))}\n"
            )

        task = Task(
            description=(
                f"Write a report on: {state['topic']} "
                f"({state.get('user_settings', {}).get('tone', 'professional')} tone).\n"
                f"{body}\n"
                f"About {target_words(state)} words, Markdown: introduction, at most 4 sections, conclusion. "
                "Use only the facts above. Cite papers as [n] and web facts with their URL. "
                "No invented facts or placeholders. Return ONLY the report."
            ),
            expected_output="Report in Markdown",
            agent=writer
        )
        crew = Crew(agents=[writer], tasks=[task], verbose=True)
        result = crew.kickoff()
        logger.info(f"{node_name} completed successfully")
        pause()
        return {"draft": str(result), "revision_round": revision_round + 1, "node": node_name}
    except Exception as e:
        logger.error(f"Error in {node_name}: {e}", exc_info=True)
        return {"error": str(e), "node": node_name}

def reviewer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    node_name = "reviewer_node"
    try:
        logger.info(f"Starting {node_name}")
        task = Task(
            description=(
                "Review the draft. Check structure, factual support against the notes, and citations "
                "(papers as [n] that exist; web URLs from the notes are also allowed).\n"
                "Reply ONLY in this format:\nVERDICT: PASS or VERDICT: REVISE\nSCORE: x/10\n"
                "FIXES: at most 3 short bullets ('none' if PASS).\n\n"
                f"Notes:\n{short(state.get('research_notes'), 1200)}\n\n"
                f"Papers:\n{format_papers(state.get('papers', []), abs_chars=120)}\n\n"
                f"Draft:\n{short(state.get('draft'), 3000)}\n"
            ),
            expected_output="VERDICT line, SCORE line and FIXES list",
            agent=reviewer
        )
        crew = Crew(agents=[reviewer], tasks=[task], verbose=True)
        result = crew.kickoff()
        logger.info(f"{node_name} completed successfully")
        pause()
        return {"feedback": {"overall_score": str(result)}, "node": node_name}
    except Exception as e:
        logger.error(f"Error in {node_name}: {e}", exc_info=True)
        return {"error": str(e), "node": node_name}

def final_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Uses the approved draft directly, or polishes it once if the reviewer still had fixes."""
    node_name = "final_node"
    try:
        logger.info(f"Starting {node_name}")
        draft = state.get("draft")
        if not draft:
            return {"error": "No draft available to finalize.", "node": node_name}

        review = str(state.get("feedback", {}).get("overall_score", "")).lower()
        if "verdict: pass" in review:
            body = draft  # already approved: no extra LLM call
        else:
            task = Task(
                description=(
                    "Polish this report. Apply the fixes, keep all [n] citations and URLs, "
                    "remove unsupported claims. Do NOT add a References section. "
                    f"Return ONLY the report.\n\nFixes:\n{fixes_only(review)}\n\n"
                    f"Draft:\n{short(draft, 3500)}"
                ),
                expected_output="Final report in Markdown",
                agent=final_agent
            )
            body = str(Crew(agents=[final_agent], tasks=[task], verbose=True).kickoff())

        return {"final_report": body + build_references(state, body), "node": node_name}
    except Exception as e:
        logger.error(f"Error in {node_name}: {e}", exc_info=True)
        return {"error": str(e), "node": node_name}

# -----------------------------------------------------------------------------
# Workflow Edges and Conditional Logic
# -----------------------------------------------------------------------------
def should_revise(state: ReportState) -> bool:
    """Another revision round is needed unless the reviewer passed the draft or the limit is reached."""
    try:
        if state.get("error"):
            return False
        if state.get("final_report"):
            return False
        feedback = state.get("feedback", {})
        review_text = str(feedback.get("overall_score", "")).lower()
        passed = "verdict: pass" in review_text
        max_rounds = int(os.environ.get("AGENT_MAX_RETRIES", 3))
        return bool(review_text) and not passed and state.get("revision_round", 0) < max_rounds
    except Exception as e:
        logger.error(f"Error in should_revise: {e}", exc_info=True)
        return False

# -----------------------------------------------------------------------------
# Workflow Graph Construction
# -----------------------------------------------------------------------------
workflow = StateGraph(ReportState)

workflow.add_node("researcher_node", researcher_node)
workflow.add_node("writer_node", writer_node)
workflow.add_node("reviewer_node", reviewer_node)
workflow.add_node("final_node", final_node)

workflow.set_entry_point("researcher_node")
workflow.add_edge("researcher_node", "writer_node")
workflow.add_edge("writer_node", "reviewer_node")

# Review loop: revise (back to writer) or hand over to the final editor
workflow.add_conditional_edges(
    "reviewer_node",
    should_revise,
    {True: "writer_node", False: "final_node"}
)
workflow.add_edge("final_node", END)

# Compile the workflow
workflow = workflow.compile()

# Global checkpointer instance for FastAPI / app imports
try:
    checkpointer = PostgreSQLCheckpointer()
except Exception as e:
    logger.warning(f"Could not connect to PostgreSQL checkpointer globally: {e}. Using mock checkpointer fallback.")
    checkpointer = MockPostgreSQLCheckpointer()

# Cache checkpointer instance for caching
try:
    cache_checkpointer = RedisCacheCheckpointer()
except Exception as e:
    logger.warning(f"Could not connect to Redis checkpointer: {e}. Using mock cache fallback.")
    cache_checkpointer = MockRedisCacheCheckpointer()

def new_state(topic: str, user_settings: dict) -> ReportState:
    return ReportState(
        topic=topic,
        user_settings=user_settings,
        research_notes="",
        papers=[],
        draft="",
        feedback={},
        final_report="",
        revision_round=0,
        node="",
        error="",
    )

def run_report_stream(topic: str, user_settings: dict = None, checkpointer=None, cache_checkpointer=None):
    """Runs the workflow and yields (event, data) pairs for SSE."""
    user_settings = user_settings or {"report_length": "detailed", "tone": "professional"}

    # Use a mock checkpointer if none is provided
    if checkpointer is None:
        checkpointer = MockPostgreSQLCheckpointer()

    # Use a mock cache checkpointer if none is provided
    if cache_checkpointer is None:
        cache_checkpointer = MockRedisCacheCheckpointer()

    # Load existing state if it exists and is not already completed
    existing_state = checkpointer.load_state(topic)
    if existing_state and existing_state.get("final_report", ""):
        logger.info(f"Loaded completed state for topic: {topic}")
        state = existing_state
    else:
        # Start fresh if no completed state or if it's incomplete
        state = new_state(topic, user_settings)

    final_state = dict(state)
    yield "start", {"topic": topic}

    try:
        for update in workflow.stream(state, stream_mode="updates"):
            for node, changes in update.items():
                if isinstance(changes, dict):
                    final_state.update(changes)
                yield "progress", {"node": node, "update": changes}

        # Save the final state
        checkpointer.save_state(topic, final_state)
        yield "done", {"state": final_state}

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        yield "error", {"message": str(e)}

# -----------------------------------------------------------------------------
# Main Execution
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("Initializing Multi-Agent System Agents (Researcher, Writer, Reviewer, Final Editor)...")

    try:
        topic = "Advancements in Quantum Computing"
        user_settings = {"report_length": "detailed", "tone": "professional"}

        initial_state = checkpointer.load_state(topic)
        if not initial_state or not initial_state.get("final_report"):
            initial_state = new_state(topic, user_settings)

        print(f"Starting workflow for topic: {topic}")
        result = workflow.invoke(initial_state)

        checkpointer.save_state(topic, result)

        print("Workflow completed successfully.")
        print(f"Final output: {result}")

    except Exception as e:
        logger.error(f"Error during execution: {e}", exc_info=True)
        import traceback
        traceback.print_exc()

    finally:
        if checkpointer is not None:
            checkpointer.close()
        if cache_checkpointer is not None:
            cache_checkpointer.close()