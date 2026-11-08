import { Fragment } from "react";
import { NODES, type NodeId } from "../plain";

export type NodeState = "ok" | "origin" | "affected" | "slow" | "down" | "unknown";

const STATE_TEXT: Record<NodeState, string> = {
  ok: "Working",
  origin: "Problem started here",
  affected: "Affected",
  slow: "Slow",
  down: "Not responding",
  unknown: "Unknown",
};

interface Props {
  states: Partial<Record<NodeId, NodeState>>;
  notes?: Partial<Record<NodeId, string>>;
  compact?: boolean;
}

/** The demo shop drawn as four boxes: User → Gateway → Orders → Inventory → Database. */
export default function SystemMap({ states, notes = {}, compact = false }: Props) {
  return (
    <div className={`system-map ${compact ? "compact" : ""}`}>
      <div className="map-user">
        <div className="map-user-icon">👤</div>
        <div className="small muted">User</div>
      </div>
      {NODES.map((node) => {
        const state = states[node.id] ?? "unknown";
        return (
          <Fragment key={node.id}>
            <div className={`map-arrow arrow-${state}`} aria-hidden>
              →
            </div>
            <div className={`map-node node-${state}`}>
              <div className="map-node-name">{node.name}</div>
              {!compact && <div className="map-node-role">{node.role}</div>}
              <div className="map-node-state">{STATE_TEXT[state]}</div>
              {notes[node.id] && <div className="map-node-note">{notes[node.id]}</div>}
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
