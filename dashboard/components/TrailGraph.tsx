import type { CaseDetail } from "@/lib/api";
import { naira, short } from "@/lib/format";

const EXIT: Record<string, string> = {
  cash: "Cash at ATM", pos_agent: "Cash at POS agent", merchant: "Paid to merchant",
  outside_network: "Left the network",
};

type Node = {
  id: string; col: number; row: number; title: string; sub: string;
  kind: "victim" | "account" | "exit"; flagged: boolean; held: boolean; received: number;
};

/** The stolen money's path, left to right: victim, mule accounts, and where it left. */
export function TrailGraph({ detail }: { detail: CaseDetail }) {
  const flagged = new Set(detail.accounts.filter((a) => a.alert).map((a) => a.account));
  const held = new Set(detail.holds.filter((h) => h.status === "active").map((h) => h.account));
  const here = new Map(detail.holdings.map((h) => [h.account, h.tainted_kobo]));

  const nodes = new Map<string, Node>();
  const edges = new Map<string, { from: string; to: string; amount: number }>();
  const rows: number[] = [];
  const add = (id: string, col: number, node: Omit<Node, "id" | "col" | "row" | "received">) => {
    if (!nodes.has(id)) {
      rows[col] = (rows[col] ?? 0) + 1;
      nodes.set(id, { id, col, row: rows[col] - 1, received: 0, ...node });
    }
    return nodes.get(id)!;
  };

  add(detail.victim_account, 0, {
    title: "Victim", sub: `${detail.reporting_bank} ${short(detail.victim_account)}`,
    kind: "victim", flagged: false, held: false,
  });
  for (const hop of detail.hops) {
    const to = hop.endpoint ? `${hop.sender_account}>${hop.endpoint}` : hop.receiver_account!;
    const node = hop.endpoint
      ? add(to, hop.hop, { title: EXIT[hop.endpoint] ?? hop.endpoint, sub: "", kind: "exit", flagged: false, held: false })
      : add(to, hop.hop, {
          title: `${hop.receiver_bank} ${short(to)}`, sub: "", kind: "account",
          flagged: flagged.has(to), held: held.has(to),
        });
    node.received += hop.tainted_kobo;
    const key = `${hop.sender_account}|${to}`;
    const edge = edges.get(key) ?? { from: hop.sender_account, to, amount: 0 };
    edge.amount += hop.tainted_kobo;
    edges.set(key, edge);
  }

  const COL = 216, ROW = 78, W = 168, H = 54, PAD = 16;
  const cols = rows.length, tall = Math.max(...rows);
  const width = PAD * 2 + (cols - 1) * COL + W, height = PAD * 2 + tall * ROW - (ROW - H);
  const x = (n: Node) => PAD + n.col * COL;
  const y = (n: Node) => PAD + n.row * ROW + ((tall - rows[n.col]) * ROW) / 2;
  const biggest = Math.max(...[...edges.values()].map((e) => e.amount), 1);

  return (
    <div className="trail scroll">
      <svg viewBox={`0 0 ${width} ${height}`} role="img"
           style={{ width: "100%", minWidth: Math.min(width, 760), maxWidth: width, height: "auto" }}
           aria-label="Path of the stolen money from the victim through mule accounts to where it left the banking system">
        {[...edges.values()].map((edge) => {
          const a = nodes.get(edge.from), b = nodes.get(edge.to);
          if (!a || !b) return null;
          const x1 = x(a) + W, y1 = y(a) + H / 2, x2 = x(b), y2 = y(b) + H / 2, mid = (x1 + x2) / 2;
          return (
            <path key={edge.from + edge.to} d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`}
                  className={b.kind === "exit" ? "edge out" : "edge"}
                  strokeWidth={1.5 + (edge.amount / biggest) * 6} />
          );
        })}
        {[...nodes.values()].map((n) => {
          const left = here.get(n.id) ?? 0;
          const box = (
            <g transform={`translate(${x(n)} ${y(n)})`}
               className={`node ${n.kind}${n.flagged ? " flagged" : ""}${n.held ? " held" : ""}`}>
              <rect width={W} height={H} rx={n.kind === "exit" ? 27 : 6} />
              <text x={12} y={22} className="t">{n.title}</text>
              <text x={12} y={41} className="s">
                {n.kind === "victim" ? `lost ${naira(detail.stolen_kobo)}`
                  : n.kind === "exit" ? naira(n.received)
                  : left > 0 ? `${naira(left)} still here` : `passed on ${naira(n.received)}`}
              </text>
            </g>
          );
          return n.kind === "account"
            ? <a key={n.id} href={`#acct-${n.id}`} aria-label={`${n.title}: show its flags and hold`}>{box}</a>
            : <g key={n.id}>{box}</g>;
        })}
      </svg>
      <p className="legend on-dark">
        <span><i className="k-flag" />Flagged by the scorer</span>
        <span><i className="k-held" />Under hold</span>
        <span><i className="k-exit" />Left the banking system</span>
      </p>
    </div>
  );
}
