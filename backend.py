"""
Backend module for the Multi-Agent Report Writer System.
Four agents (Researcher, Writer, Reviewer, Final Editor) orchestrated with LangGraph,
CrewAI + Groq LLM, PostgreSQL state checkpointer, and Redis cache.

Workflow:
    researcher_node -> writer_node -> reviewer_node --(revise)--> writer_node
                                                    \--(done)---> final_node -> END
"""
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Any, Dict, List, TypedDict

import psycopg2
import psycopg2.extras
import redis
from crewai import Agent, Crew, LLM, Task
from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from tools.tavily_search import TavilySearchTool

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

WORD_TARGETS = {"short": 300, "detailed": 450, "comprehensive": 700}


def short(value: Any, n: int = 1500) -> str:
    """Cut long text so each request stays small."""
    return str(value)[:n]


def fixes_only(review: str, n: int = 600) -> str:
    """Keep only the FIXES part of the reviewer output, not the whole review."""
    parts = re.split(r"FIXES:", str(review), flags=re.I)
    return short(parts[-1].strip() if len(parts) > 1 else review, n)


def pause() -> None:
    """Small delay between LLM calls to stay inside free-tier rate limits."""
    delay = float(os.environ.get("AGENT_STEP_DELAY", 5))
    if delay > 0:
        time.sleep(delay)


def target_words(state: Dict[str, Any]) -> int:
    """Hard word target so the writer never produces an oversized report."""
    level = str(state.get("user_settings", {}).get("report_length", "detailed")).lower()
    return WORD_TARGETS.get(level, 450)


def make_state_key(topic: str, user_settings: Dict[str, Any]) -> str:
    """Composite key so different length/tone settings for the same topic don't collide."""
    length = str(user_settings.get("report_length", "detailed")).lower()
    tone = str(user_settings.get("tone", "professional")).lower()
    return f"{topic.strip()}::{length}::{tone}"


def search_arxiv(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Real paper search (arXiv, no API key). Only these papers may be cited."""
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
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


def format_papers(papers: List[Dict[str, Any]], limit: int = 2, abs_chars: int = 250) -> str:
    """Compact numbered paper list: at most 2 papers, short abstracts."""
    if not papers:
        return "No papers. Do not cite any."
    return "\n".join(
        f"[{i}] {p['authors'][0] if p['authors'] else 'Unknown'} et al. ({p['year']}). "
        f"{p['title']}. Abstract: {p['abstract'][:abs_chars]}"
        for i, p in enumerate(papers[:limit], 1)
    )


def build_references(state: Dict[str, Any], draft: str) -> str:
    """References are built in Python, so the LLM never has to write them."""
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", draft)}
    lines = [
        f"[{i}] {', '.join(p['authors'][:3])} ({p['year']}). {p['title']}. {p['url']}"
        for i, p in enumerate(state.get("papers", [])[:2], 1)
        if i in cited
    ]
    raw_urls = re.findall(r"https?://[^\s】\)\]]+", state.get("research_notes", ""))
    urls = sorted({u.rstrip(".,;:>\"'") for u in raw_urls})

    out = ""
    if lines:
        out += "\n\n## References\n" + "\n".join(lines)
    if urls:
        out += "\n\n**Web sources**\n" + "\n".join(f"- {u}" for u in urls)
    return out


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
# PostgreSQL Checkpointer with Reconnect & Retry Logic
# -----------------------------------------------------------------------------
class PostgreSQLCheckpointer:
    def __init__(self):
        self.conn = None
        self._initialize_table()

    def _connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(
                os.environ.get(
                    "POSTGRES_HOST",
                    "postgres://postgres:postgres@localhost/multi_agent_writer",
                ),
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=5,
            )

    def _reset_conn(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def _initialize_table(self):
        """Initialize the PostgreSQL table with retry logic."""
        max_retries = 3
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                self._connect()
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS workflow_states (
                            topic TEXT PRIMARY KEY,
                            state JSONB NOT NULL,
                            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                self.conn.commit()
                logger.info("PostgreSQL table initialized successfully.")
                return
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed to connect to PostgreSQL: {e}")
                self._reset_conn()
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Failed to initialize PostgreSQL after retries.")
                    raise

    def save_state(self, topic: str, state: Dict[str, Any]) -> None:
        """Save state to PostgreSQL with automatic reconnect & retry."""
        max_retries = 3
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                self._connect()
                with self.conn.cursor() as cursor:
                    payload = json.dumps(state)
                    cursor.execute(
                        "INSERT INTO workflow_states (topic, state) VALUES (%s, %s) "
                        "ON CONFLICT (topic) DO UPDATE SET state = %s, timestamp = CURRENT_TIMESTAMP",
                        (topic, payload, payload),
                    )
                self.conn.commit()
                return
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed to save state: {e}")
                self._reset_conn()
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    raise

    def load_state(self, topic: str) -> Dict[str, Any]:
        """Load state from PostgreSQL with automatic reconnect & retry."""
        max_retries = 3
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                self._connect()
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT state FROM workflow_states WHERE topic = %s",
                        (topic,),
                    )
                    result = cursor.fetchone()
                    if result:
                        state = result["state"]
                        return state if isinstance(state, dict) else json.loads(state)
                    return {}
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed to load state: {e}")
                self._reset_conn()
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    raise

    def close(self) -> None:
        """Close the PostgreSQL connection."""
        if self.conn and not self.conn.closed:
            self.conn.close()
            self.conn = None
            logger.info("PostgreSQL connection closed.")


class MockPostgreSQLCheckpointer:
    def __init__(self):
        self.states: Dict[str, Dict[str, Any]] = {}

    def save_state(self, topic: str, state: Dict[str, Any]) -> None:
        self.states[topic] = state

    def load_state(self, topic: str) -> Dict[str, Any]:
        return self.states.get(topic, {})

    def close(self) -> None:
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
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                self.redis_conn = redis.Redis.from_url(
                    os.environ.get("CACHE_REDIS_URL", "redis://localhost:6379/0"),
                    socket_connect_timeout=5,
                )
                self.redis_conn.ping()
                logger.info("Redis connection initialized successfully.")
                return
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed to connect to Redis: {e}")
                self.redis_conn = None
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Failed to initialize Redis after retries.")
                    raise

    def save_state(self, topic: str, state: Dict[str, Any], ttl_seconds: int = 86400) -> None:
        """Save state to Redis with TTL."""
        if not self.redis_conn:
            raise RuntimeError("Redis connection not established.")
        try:
            self.redis_conn.setex(f"{topic}:state", ttl_seconds, json.dumps(state))
            logger.info(f"State saved to Redis for key: {topic}")
        except Exception as e:
            logger.error(f"Failed to save state to Redis: {e}")

    def load_state(self, topic: str) -> Dict[str, Any]:
        """Load state from Redis."""
        if not self.redis_conn:
            raise RuntimeError("Redis connection not established.")
        try:
            state_str = self.redis_conn.get(f"{topic}:state")
            return json.loads(state_str) if state_str else {}
        except Exception as e:
            logger.error(f"Failed to load state from Redis: {e}")
            return {}

    def close(self) -> None:
        if self.redis_conn:
            self.redis_conn.close()
            self.redis_conn = None
            logger.info("Redis connection closed.")


class MockRedisCacheCheckpointer:
    def __init__(self):
        self.states: Dict[str, Dict[str, Any]] = {}

    def save_state(self, topic: str, state: Dict[str, Any]) -> None:
        self.states[topic] = state

    def load_state(self, topic: str) -> Dict[str, Any]:
        return self.states.get(topic, {})

    def close(self) -> None:
        pass


# -----------------------------------------------------------------------------
# Lazy Checkpointer Factories
# -----------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_checkpointer():
    try:
        return PostgreSQLCheckpointer()
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}). Using in-memory fallback.")
        return MockPostgreSQLCheckpointer()


@lru_cache(maxsize=1)
def get_cache_checkpointer():
    if os.environ.get("CACHE_ENABLED", "True").lower() != "true":
        return MockRedisCacheCheckpointer()
    try:
        return RedisCacheCheckpointer()
    except Exception as e:
        logger.warning(f"Redis unavailable ({e}). Using in-memory cache fallback.")
        return MockRedisCacheCheckpointer()


# -----------------------------------------------------------------------------
# LLM and Lazy Agent Factories
# -----------------------------------------------------------------------------
def get_groq_llm(temperature=None, max_tokens=None, model=None) -> LLM:
    kwargs = {
        "model": model or os.environ.get("GROQ_MODEL_NAME", "groq/openai/gpt-oss-20b"),
        "api_key": os.environ.get("GROQ_API_KEY"),
        "max_tokens": max_tokens or int(os.environ.get("MAX_OUTPUT_TOKENS", 2000)),
        "reasoning_effort": "low",
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return LLM(**kwargs)


@lru_cache(maxsize=1)
def get_researcher_agent() -> Agent:
    return Agent(
        role="Senior Research Analyst",
        goal="Break the topic into key questions and find authoritative, current information on each.",
        backstory=(
            "You are an expert researcher who plans a short list of key questions for a topic, "
            "then digs into the web to find authoritative, current and relevant information. "
            "You only report facts you actually found, and you always keep the source."
        ),
        tools=[TavilySearchTool()],
        max_iter=3,
        llm=get_groq_llm(max_tokens=1500),
        verbose=True,
        allow_delegation=False,
    )


@lru_cache(maxsize=1)
def get_writer_agent() -> Agent:
    return Agent(
        role="Professional Report Writer",
        goal="Transform research notes into a coherent, well-structured, and readable report.",
        backstory=(
            "You are an expert technical writer known for your clarity and attention to detail. "
            "You synthesize research into professional, engaging reports, cite sources accurately, "
            "and never invent facts. When you receive reviewer feedback you fix every issue it lists."
        ),
        llm=get_groq_llm(max_tokens=2500),
        verbose=True,
        allow_delegation=False,
    )


@lru_cache(maxsize=1)
def get_reviewer_agent() -> Agent:
    return Agent(
        role="Technical Reviewer",
        goal="Evaluate report quality and provide actionable feedback for improvement.",
        backstory=(
            "You are a meticulous technical reviewer with a keen eye for detail. "
            "You assess structure, coverage, factual support, citations and readability, "
            "and give short, specific, constructive feedback."
        ),
        llm=get_groq_llm(
            temperature=0.2,
            max_tokens=800,
            model=os.environ.get("REVIEWER_MODEL"),
        ),
        verbose=True,
        allow_delegation=False,
    )


@lru_cache(maxsize=1)
def get_final_agent() -> Agent:
    return Agent(
        role="Final Report Editor",
        goal="Combine the research notes, papers, approved draft and reviewer feedback into one polished final report.",
        backstory=(
            "You are a master editor. You take the work of a researcher, a writer and a reviewer and "
            "merge it into a single, consistent, professional document. You apply any remaining "
            "reviewer fixes, keep every fact and citation that is supported, remove anything unsupported, "
            "and finish with an accurate reference list. You never invent facts, sources or numbers."
        ),
        llm=get_groq_llm(temperature=0.3, max_tokens=2500),
        verbose=True,
        allow_delegation=False,
    )


# -----------------------------------------------------------------------------
# Workflow Nodes
# -----------------------------------------------------------------------------
def researcher_node(state: Dict[str, Any]) -> Dict[str, Any]:
    node_name = "researcher_node"
    try:
        logger.info(f"Starting {node_name}")
        papers = search_arxiv(state["topic"], max_results=3)
        agent = get_researcher_agent()
        task = Task(
            description=(
                f"Topic: {state['topic']}\n"
                f"Pick at most 3 key questions for a ~{target_words(state)} word report. "
                "Use the tavily_search tool ONCE. Return at most 8 bullets, each under 20 words, "
                "grouped by question, each ending with its source URL. "
                "Only facts found in results. No questions or offers."
            ),
            expected_output="Short research notes grouped by key question, with source URLs",
            agent=agent,
        )
        result = Crew(agents=[agent], tasks=[task], verbose=True).kickoff()
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
        agent = get_writer_agent()

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
            agent=agent,
        )
        result = Crew(agents=[agent], tasks=[task], verbose=True).kickoff()
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
        agent = get_reviewer_agent()
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
            agent=agent,
        )
        result = Crew(agents=[agent], tasks=[task], verbose=True).kickoff()
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

        raw_review = str(state.get("feedback", {}).get("overall_score", ""))
        if "verdict: pass" in raw_review.lower():
            body = draft
        else:
            agent = get_final_agent()
            task = Task(
                description=(
                    "Polish this report. Apply the fixes, keep all [n] citations and URLs, "
                    "remove unsupported claims. Do NOT add a References section. "
                    f"Return ONLY the report.\n\nFixes:\n{fixes_only(raw_review)}\n\n"
                    f"Draft:\n{short(draft, 3500)}"
                ),
                expected_output="Final report in Markdown",
                agent=agent,
            )
            body = str(Crew(agents=[agent], tasks=[task], verbose=True).kickoff())

        return {"final_report": body + build_references(state, body), "node": node_name}
    except Exception as e:
        logger.error(f"Error in {node_name}: {e}", exc_info=True)
        return {"error": str(e), "node": node_name}


# -----------------------------------------------------------------------------
# Workflow Routing & Graph Construction
# -----------------------------------------------------------------------------
def route_on_error(state: ReportState) -> str:
    return "error" if state.get("error") else "ok"


def route_after_review(state: ReportState) -> str:
    if state.get("error") or state.get("final_report"):
        return "end"
    review_text = str(state.get("feedback", {}).get("overall_score", "")).lower()
    passed = "verdict: pass" in review_text
    max_rounds = int(os.environ.get("AGENT_MAX_RETRIES", 1))
    if review_text and not passed and state.get("revision_round", 0) < max_rounds:
        return "revise"
    return "finalize"


graph = StateGraph(ReportState)

graph.add_node("researcher_node", researcher_node)
graph.add_node("writer_node", writer_node)
graph.add_node("reviewer_node", reviewer_node)
graph.add_node("final_node", final_node)

graph.set_entry_point("researcher_node")
graph.add_conditional_edges(
    "researcher_node",
    route_on_error,
    {"ok": "writer_node", "error": END},
)
graph.add_conditional_edges(
    "writer_node",
    route_on_error,
    {"ok": "reviewer_node", "error": END},
)
graph.add_conditional_edges(
    "reviewer_node",
    route_after_review,
    {"revise": "writer_node", "finalize": "final_node", "end": END},
)
graph.add_edge("final_node", END)

workflow = graph.compile()


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


def _load_cached_report(key: str, cp, cache_cp) -> Dict[str, Any]:
    """Check Redis first, then PostgreSQL. Backfill Redis if found in Postgres."""
    cached = cache_cp.load_state(key)
    if cached and cached.get("final_report"):
        return cached

    stored = cp.load_state(key)
    if stored and stored.get("final_report"):
        cache_cp.save_state(key, stored)
        return stored

    return {}


def run_report_stream(
    topic: str,
    user_settings: dict = None,
    checkpointer=None,
    cache_checkpointer=None,
):
    """Runs the workflow and yields (event, data) pairs for SSE."""
    user_settings = user_settings or {"report_length": "detailed", "tone": "professional"}
    cp = checkpointer or get_checkpointer()
    cache_cp = cache_checkpointer or get_cache_checkpointer()
    key = make_state_key(topic, user_settings)

    yield "start", {"topic": topic}

    existing_state = _load_cached_report(key, cp, cache_cp)
    if existing_state:
        logger.info(f"Serving cached report for key: {key}")
        yield "done", {"state": existing_state}
        return

    state = new_state(topic, user_settings)
    final_state = dict(state)

    try:
        for update in workflow.stream(state, stream_mode="updates"):
            for node, changes in update.items():
                if isinstance(changes, dict):
                    final_state.update(changes)
                yield "progress", {"node": node, "update": changes}

        if final_state.get("error"):
            yield "error", {"message": final_state["error"]}
            return

        cp.save_state(key, final_state)
        cache_cp.save_state(key, final_state)
        yield "done", {"state": final_state}

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        yield "error", {"message": str(e)}


# -----------------------------------------------------------------------------
# Main Execution
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    cp = get_checkpointer()
    cache_cp = get_cache_checkpointer()

    try:
        topic = "Advancements in Quantum Computing"
        user_settings = {"report_length": "detailed", "tone": "professional"}
        key = make_state_key(topic, user_settings)

        cached = _load_cached_report(key, cp, cache_cp)
        if cached:
            print("Loaded cached report.")
            result = cached
        else:
            print(f"Starting workflow for topic: {topic}")
            result = workflow.invoke(new_state(topic, user_settings))
            if not result.get("error"):
                cp.save_state(key, result)
                cache_cp.save_state(key, result)

        if result.get("error"):
            print(f"Workflow failed at {result.get('node')}: {result['error']}")
        else:
            print("Workflow completed successfully.")
            print(result.get("final_report", ""))

    finally:
        cp.close()
        cache_cp.close()