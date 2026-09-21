// src/components/dashboard/CandleFlowIndicator.tsx
//
// FIX 2026-09-20: per-stream candle-flow indicator on the dashboard.
//
// Before this, users had no way to know whether their bot was actively
// receiving candles for a given strategy without tailing container logs.
// This component surfaces a GREEN/YELLOW/RED dot per (symbol, timeframe)
// stream, plus a "last seen Xs ago" sublabel. If any stream is silent
// for too long, the whole card tints amber so the user notices even at a
// glance.
//
// Data comes from useCandleHealth which polls /api/v1/market-data/candle-health.
// Pure UI; no new API calls beyond what the hook already does.

import { useMemo } from "react";
import { Activity, AlertCircle, CheckCircle2, Clock } from "lucide-react";
import { useCandleHealth } from "@/lib/api";
import { usePortfolioMode } from "@/context/PortfolioModeContext";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";

type Status = "live" | "stale" | "silent" | "unknown";

const STATUS_META: Record<
	Status,
	{
		label: string;
		dot: string; // tailwind bg color for the dot
		text: string; // text color
		ring: string; // ring around the dot
		Icon: typeof Activity;
	}
> = {
	live: {
		label: "Live",
		dot: "bg-emerald-500",
		text: "text-emerald-400",
		ring: "ring-emerald-500/30",
		Icon: CheckCircle2,
	},
	stale: {
		label: "Stale",
		dot: "bg-amber-500",
		text: "text-amber-400",
		ring: "ring-amber-500/30",
		Icon: Clock,
	},
	silent: {
		label: "Silent",
		dot: "bg-rose-500",
		text: "text-rose-400",
		ring: "ring-rose-500/30",
		Icon: AlertCircle,
	},
	unknown: {
		label: "Starting…",
		dot: "bg-slate-400",
		text: "text-muted-foreground",
		ring: "ring-slate-500/30",
		Icon: Activity,
	},
};

function formatAge(seconds: number | null | undefined): string {
	if (seconds == null) return "—";
	if (seconds < 60) return `${Math.round(seconds)}s ago`;
	if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
	if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
	return `${Math.round(seconds / 86400)}d ago`;
}

export const CandleFlowIndicator = () => {
	const { mode } = usePortfolioMode();
	const { data, isLoading } = useCandleHealth({ mode });
	const streams = data?.streams ?? [];

	// Count statuses for the header summary
	const summary = useMemo(() => {
		const counts: Record<Status, number> = {
			live: 0,
			stale: 0,
			silent: 0,
			unknown: 0,
		};
		for (const s of streams) {
			counts[s.status] = (counts[s.status] ?? 0) + 1;
		}
		return counts;
	}, [streams]);

	const hasIssue = summary.silent > 0 || summary.stale > 0;
	const hasStreams = streams.length > 0;

	return (
		<Card
			className={
				hasIssue
					? "relative overflow-hidden border-amber-500/40 bg-gradient-to-br from-amber-500/5 via-card/40 to-card/40"
					: "relative overflow-hidden border-emerald-500/30 bg-gradient-to-br from-emerald-500/5 via-card/40 to-card/40"
			}
		>
			<CardContent className="relative px-3 py-3 sm:px-4">
				<div className="flex items-center justify-between gap-2 sm:gap-3 flex-wrap">
					<div className="flex items-center gap-3 min-w-0">
						<Badge
							variant="outline"
							className={
								hasIssue
									? "bg-amber-500/10 text-amber-400 border-amber-500/30 font-semibold tracking-wide"
									: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30 font-semibold tracking-wide"
							}
						>
							<span className="relative flex h-2 w-2 mr-1.5">
								{hasIssue ? (
									<span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
								) : (
									<>
										<span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
										<span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400" />
									</>
								)}
							</span>
							<Activity className="w-3 h-3 mr-1 inline-block" />
							CANDLE FLOW
						</Badge>
						<span className="text-sm text-muted-foreground truncate">
							{isLoading ? (
								<>Checking streams…</>
							) : !hasStreams ? (
								<>No active streams — start a strategy to see candle flow.</>
							) : (
								<>
									<span className="font-semibold text-foreground tabular-nums">
										{summary.live}
									</span>{" "}
									live ·{" "}
									<span className="font-semibold text-amber-400 tabular-nums">
										{summary.stale}
									</span>{" "}
									stale ·{" "}
									<span className="font-semibold text-rose-400 tabular-nums">
										{summary.silent}
									</span>{" "}
									silent
								</>
							)}
						</span>
					</div>
				</div>

				{/* Per-stream chips. Wraps on narrow viewports. */}
				{hasStreams && (
					<div className="mt-3 flex flex-wrap gap-2">
						{streams.map((s) => {
							const meta = STATUS_META[s.status] ?? STATUS_META.unknown;
							const Icon = meta.Icon;
							return (
								<div
									key={`${s.exchange}:${s.market_type}:${s.symbol}:${s.timeframe}`}
									className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs bg-card/60 ${meta.ring}`}
									title={
										s.last_candle_ts_ms
											? `Last candle at ${new Date(s.last_candle_ts_ms).toISOString()}`
											: "No heartbeat yet"
									}
								>
									<span
										className={`inline-block h-1.5 w-1.5 rounded-full ${meta.dot}`}
									/>
									<Icon className={`w-3 h-3 ${meta.text}`} />
									<span className="font-medium tabular-nums">{s.symbol}</span>
									<span className="text-muted-foreground">{s.timeframe}</span>
									<span className="text-muted-foreground">
										· {formatAge(s.seconds_since_last_candle)}
									</span>
									<span className="sr-only">{meta.label}</span>
								</div>
							);
						})}
					</div>
				)}
			</CardContent>
		</Card>
	);
};

export default CandleFlowIndicator;
