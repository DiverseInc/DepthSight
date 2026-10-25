// src/components/dashboard/TradingDisabledBanner.tsx

import { AlertTriangle } from "lucide-react";
import type React from "react";
import { useTranslation } from "react-i18next";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";

interface TradingDisabledBannerProps {
	/** `false` means the bot is not allowed to open new positions right now. */
	isTradingAllowed?: boolean;
	/**
	 * The reason the backend gave for blocking trading, e.g.
	 * "DailyLoss: -120.00 USD <= -50.00 USD". The backend already computes
	 * this (`trading_disabled_reason` on the portfolio status payload), so we
	 * show it verbatim instead of inventing a message.
	 */
	reason?: string | null;
	className?: string;
}

/**
 * Tells the client, in plain language, that the bot has stopped opening new
 * positions -- and why.
 *
 * The backend has always computed and returned this reason; the frontend
 * simply threw it away, so a client whose bot was blocked by the daily loss
 * limit saw a normal-looking dashboard with no explanation. The support KB
 * even promises this behaviour ("the bot blocks all new entries"), so the
 * product was contradicting its own documentation.
 *
 * Styled like the rest of the risk family: the same red `cannot_trade` badge
 * used by TopStrategiesTable, inside the same destructive Alert used by the
 * dashboard's other error states.
 */
export const TradingDisabledBanner: React.FC<TradingDisabledBannerProps> = ({
	isTradingAllowed,
	reason,
	className,
}) => {
	const { t } = useTranslation(["index", "common"]);

	// Only show the banner when trading is explicitly blocked.
	if (isTradingAllowed !== false) return null;

	const trimmedReason = reason?.trim();

	return (
		<Alert variant="destructive" className={className}>
			<AlertTriangle className="h-4 w-4" />
			<AlertTitle className="flex flex-wrap items-center gap-2">
				{t("index:tradingDisabled.title")}
				<Badge className="border-transparent bg-red-500 text-white hover:bg-red-600">
					{t("index:tradingDisabled.badge")}
				</Badge>
			</AlertTitle>
			<AlertDescription>
				{trimmedReason
					? trimmedReason
					: t("index:tradingDisabled.fallbackReason")}
			</AlertDescription>
		</Alert>
	);
};
