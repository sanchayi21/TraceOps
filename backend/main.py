from fastapi import FastAPI

app = FastAPI(title="TraceOps")


@app.get("/health")  #door we knock on and backend answerif its running
def health_check():
    return {"status": "ok", "service": "traceops"}