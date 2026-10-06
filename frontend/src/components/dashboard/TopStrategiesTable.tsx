import { formatDistanceToNow } from "date-fns";
import { enUS, ru } from "date-fns/locale";
import { AlertTriangle, Sparkles, TrendingUp } from "lucide-react";
import type React from "react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import { usePortfolioMode } from "@/context/PortfolioModeContext";
import { useStrategies } from "@/lib/api";

const getStatusBadgeVariant = (status: string) => {
	switch (status.toLowerCase()) {
		case "running":
		case "active":
			return "bg-green-500 hover:bg-green-600";
		// A live position is the strongest "working" signal there is. This
		// fell through to the grey default before, which made the one strategy
		// actually trading look idle.
		case "in_position":
			return "bg-green-600 hover:bg-green-700 text-white";
		case "stopped":
		case "paused":
			return "bg-yellow-500 hover:bg-yellow-600";
		case "error":
		// The engine detected a strategy that structurally cannot signal.
		// Red, not grey: it is broken, not merely idle.
		case "cannot_trade":
			return "bg-red-500 hover:bg-red-600";
		default:
			return "bg-gray-500 hover:bg-gray-600";
	}
};

import { useAccountStore } from "@/stores/accountStore";

export const TopStrategiesTable: React.FC<{ topN?: number }> = ({
	topN = 5,
}) => {
	const { mode } = usePortfolioMode();
	const { selectedApiKeyId } = useAccountStore();
	const { t, i18n } = useTranslation(["index", "common"]);
	const {
		data: strategies,
		isLoading,
		isError,
		error,
	} = useStrategies({
		mode,
		apiKeyId: mode === "live" ? selectedApiKeyId : undefined,
	});
	const navigate = useNavigate();

	const dateFnsLocale = useMemo(() => {
		const lang = i18n.language.split("-")[0];
		if (lang === "ru") return ru;
		return enUS;
	}, [i18n.language]);

	const topStrategies = useMemo(() => {
		if (!strategies) return [];
		// A strategy holding a live position is pinned into view and is NEVER
		// truncated away, regardless of rank.
		//
		// Ranking purely by PnL and slicing dropped exactly the row that
		// matters most: a position-holding strategy is usually the ONLY one
		// with a non-zero PnL, and on this account it was the only one that
		// had any at all, so it always sorted LAST and was the first row cut.
		// With more strategies added it would have kept losing rows, silently
		// hiding open risk from the dashboard.
		const holdsPosition = (s: (typeof strategies)[number]) =>
			s.status === "in_position" || (s.open_positions ?? 0) > 0;
		const holding = strategies.filter(holdsPosition);
		const rest = strategies.filter((s) => !holdsPosition(s));
		rest.sort((a, b) => (b.pnl || 0) - (a.pnl || 0)); // Sort by PnL descending
		return [...holding, ...rest].slice(0, Math.max(topN, holding.length));
	}, [strategies, topN]);

	const handleRowClick = (strategyId: string) => {
		navigate(`/strategies?id=${encodeURIComponent(strategyId)}`);
	};

	const calculateRuntime = (startTime: string): string => {
		try {
			return formatDistanceToNow(new Date(startTime), {
				addSuffix: true,
				locale: dateFnsLocale,
			});
		} catch {
			return t("common:na");
		}
	};

	if (isError) {
		return (
			<Alert variant="destructive" className="my-4">
				<AlertTriangle className="h-4 w-4" />
				<AlertTitle>{t("index:topStrategies.errors.loadFailed")}</AlertTitle>
				<AlertDescription>
					{error?.message || t("common:errors.unknownError")}
				</AlertDescription>
			</Alert>
		);
	}

	return (
		<Card>
			<CardHeader>
				<CardTitle className="flex items-center">
					<TrendingUp className="w-5 h-5 mr-2 text-primary" />
					{t("index:topStrategies.title")}
				</CardTitle>
				<CardDescription>
					{t("index:topStrategies.description")}
				</CardDescription>
			</CardHeader>
			<CardContent className="p-3 sm:p-6">
				{/* FIX 2026-09-21: mobile-responsive table wrapper. See note in
					ActivePositionsTable — same pattern, narrower min-width since
					this table only has 5 columns. */}
				<div className="overflow-x-auto rounded-md border">
					<Table className="min-w-[480px]">
						<TableHeader>
							<TableRow>
								<TableHead className="whitespace-nowrap">
									{t("index:topStrategies.colName")}
								</TableHead>
								<TableHead className="whitespace-nowrap">
									{t("index:topStrategies.colSymbol")}
								</TableHead>
								<TableHead className="text-center whitespace-nowrap">
									{t("index:topStrategies.colStatus")}
								</TableHead>
								<TableHead className="text-right whitespace-nowrap">
									{t("index:topStrategies.colPnl")}
								</TableHead>
								<TableHead className="text-right whitespace-nowrap">
									{t("index:topStrategies.colRuntime")}
								</TableHead>
							</TableRow>
						</TableHeader>
					<TableBody>
						{isLoading ? (
							[...Array(topN)].map((_, i) => (
								<TableRow key={`skeleton-${i}`}>
									<TableCell colSpan={5}>
										<Skeleton className="h-8 w-full" />
									</TableCell>
								</TableRow>
							))
						) : topStrategies.length === 0 ? (
							<TableRow>
								<TableCell colSpan={5} className="h-32 text-center">
									<div className="flex flex-col items-center justify-center gap-2 py-4">
										<Sparkles className="w-8 h-8 text-muted-foreground/50" />
										<p className="text-sm font-medium text-foreground">
											{t("index:topStrategies.noData")}
										</p>
										<p className="text-xs text-muted-foreground max-w-xs">
											{t("index:topStrategies.noDataHint")}
										</p>
										<Button asChild variant="outline" size="sm" className="mt-1">
											<Link to="/discovery">
												{t("index:topStrategies.exploreDiscovery")}
											</Link>
										</Button>
									</div>
								</TableCell>
							</TableRow>
						) : (
							topStrategies.map((strategy) => (
								<TableRow
									key={strategy.id}
									onClick={() => handleRowClick(strategy.id)}
									className="cursor-pointer hover:bg-muted/50"
								>
									<TableCell className="font-medium">
										{strategy.name || strategy.strategy_name}
									</TableCell>
									<TableCell className="font-mono text-sm">
										{strategy.symbol}
									</TableCell>
									<TableCell className="text-center">
										<Badge className={getStatusBadgeVariant(strategy.status)} title={strategy.status_detail || undefined}>
											{strategy.status.replace(/_/g, " ").toUpperCase()}
										</Badge>
									</TableCell>
									<TableCell
										className={`text-right font-medium mono ${strategy.pnl >= 0 ? "text-profit" : "text-loss"}`}
									>
										{strategy.pnl >= 0 ? "+" : ""}
										{strategy.pnl.toFixed(2)}
									</TableCell>
									<TableCell className="text-right text-sm text-muted-foreground">
										{calculateRuntime(strategy.started_at)}
									</TableCell>
								</TableRow>
							))
						)}
					</TableBody>
				</Table>
				</div>
			</CardContent>
		</Card>
	);
};
