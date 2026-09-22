/**
 * FirstTradeCelebration
 * ─────────────────────
 * One-time toast when the user closes their first paper or live trade.
 * Subscribes to the most recent trades in both modes; if ANY closed trade
 * exists AND we haven't celebrated this browser yet, fires a toast.
 *
 * Tracking: a single localStorage key `depthsight_first_close_v1` flags
 * "celebrated". Cross-device, the user may see it again on a new browser,
 * which is fine — it's still a meaningful moment for that session.
 *
 * Why client-side instead of backend-flagged:
 *  - Trade history endpoint already returns closed trades with `pnl` +
 *    `timestamp_close`, so we can detect without a new API.
 *  - Avoids per-user DB schema changes for a v1 polish.
 *  - WS PnL push (when 525 ticket resolves) can later swap the trigger
 *    for instant detection — same component contract.
 */
import { useEffect } from "react";
import { Sparkles } from "lucide-react";
import { useTradeHistory } from "@/lib/api";
import { useToast } from "@/components/ui/use-toast";

const SEEN_KEY = "depthsight_first_close_v1";

export const FirstTradeCelebration = () => {
    const { data: paperData } = useTradeHistory({
        mode: "paper",
        limit: 5,
        skip: 0,
    });
    const { data: liveData } = useTradeHistory({
        mode: "live",
        limit: 5,
        skip: 0,
    });
    const { toast } = useToast();

    useEffect(() => {
        // Already celebrated this browser → skip
        try {
            if (localStorage.getItem(SEEN_KEY) === "true") return;
        } catch {
            /* private mode / disabled storage — fall through and try to celebrate */
        }

        // Look for any closed trade across both modes. `timestamp_close > 0`
        // means the trade has actually closed; `pnl` is defined for closed
        // records (undefined for open positions).
        const candidates = [
            ...(paperData?.trades ?? []),
            ...(liveData?.trades ?? []),
        ].filter((t) => t.pnl !== undefined && t.timestamp_close > 0);

        if (candidates.length === 0) return;

        // API returns trades sorted by timestamp_close DESC, so [0] is the
        // most recent close. For the celebration copy we want the freshest
        // one — it matches what the user just saw on the dashboard.
        const trade = candidates[0];
        const pnl = trade.pnl as number;
        const isWin = pnl > 0;
        const abs = Math.abs(pnl);
        const formatted = `${pnl >= 0 ? "+" : "-"}$${abs.toFixed(2)}`;

        toast({
            title: (
                <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-amber-500" />
                    {isWin ? "First winning trade!" : "First trade closed"}
                </div>
            ) as unknown as string,
            description: isWin
                ? `${trade.symbol} closed at ${formatted} USD. Welcome to the markets — keep it rolling.`
                : `${trade.symbol} closed at ${formatted} USD. Every round is data — keep iterating.`,
            duration: 9000,
        });

        try {
            localStorage.setItem(SEEN_KEY, "true");
        } catch {
            /* if storage is full or disabled, don't block */
        }
    }, [paperData, liveData, toast]);

    return null;
};
