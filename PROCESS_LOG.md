# Process Log

## AI Tools Used

I used Claude Code (Sonnet 5.5) for development. No code was adapted from external sources.

## Development Narrative

I started with writing my AGENTS.md. This included information on an overview of the project I'm building, the tech stack and the skeletal structure of the repo, which I finalized with Opus 5.5, and rules to follow. I referred to various AGENTS.md online, and I saw one example that divided rules into "do freely", "ask before doing", and "never do". I started the building process with the MCP servers. I planned with Opus 5.5 the tool calls the given MCP should have (what the tool call does, what it needs as inputs, and what it should output) and used this to prompt Sonnet 5.5 to code it. I used this same process for the fred and market_data MCP servers. I then built the agent with LangGraph's StateGraph, connecting an agent node to a ToolNode with tools_condition to handle tool calls and loop back. I used MemorySaver to maintain conversation state by session_id and langchain-mcp-adapters to list and call MCP tools. I then wired in Tavily. Tavily was different because I didn't build it. They host their own MCP server, so I connected over HTTP with my API key. I wrapped everything in a Docker image and deployed it to Cloud Run. I made sure to pass my API keys from the shell through --set-env-vars so nothing sensitive is leaked. I tested the container locally first, then confirmed the live URL with the same queries I'd been running against localhost. Planning with Opus 5.5 and executing with Sonnet 5.5 worked well: the detailed prompts Opus produced let Sonnet build each file quickly and correctly. But I had to read every prompt before sending it. For the error handling, Opus's prompt said in one place to drop bad values from a payload and later to treat the whole payload as malformed. My manual review caught the contradiction before Sonnet had to guess.

## Verification Narrative

After each task, I had Sonnet 5.5 write unit tests for the code that was just written. I then gave those tests to Opus 5.5 to check whether they actually caught bugs. The prompt was: "Here are the tests Sonnet wrote for agent.py. For each test, tell me what change to agent.py would make it fail. If no change would make it fail, the test isn't testing anything." This proved to be useful. One time Opus found that one of the Sonnet-written test cases would still pass even if tools were never bound to the LLM. I also used MCP Inspector to verify that each server exposed the expected tools. I used Sonnet to compare the logs against the expected tool calls for each query and make sure there were no discrepancies.

To test the three failure modes, I had Sonnet build two deliberately broken MCP servers in tests/servers/. One exits mid-call and prints garbage and the other returns a result that doesn't match the expected output format. I ran both through FastAPI, the real MultiServerMCPClient, and the graph rather than using mocks.

Once deployed on Cloud Run, I used Postman to run queries against the live URL. I logged the HTTP status codes, the response, and the tools called.

## What I Learned

AI can write code quickly and even write test cases to see whether the code written is correct, but these test cases must be taken with a grain of salt. The LLM tool binding test case was an example where I saw that a test case passed but it didn't actually catch the bug it was testing for. I also saw that AI is good at following directions but not as good at giving them. When using Claude's plan mode, the prompt would be very detailed but it did have contradictions at times. Contradicting instructions can lead to ambiguous execution (which instruction will the agent decide to follow), which is the exact type of behavior we want to avoid when writing code with AI.
