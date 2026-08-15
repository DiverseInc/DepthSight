// src/components/dashboard/XpLevelCard.tsx
//
// Compact gamification widget: shows the user's current level, XP, and
// progress to the next level. Pure display — XP/level values come from the
// User object exposed by AuthContext, so no separate API call needed.
//
// XP curve (designed to match the demo seed's range 0-5000 XP / level 1-15):
//   xpForLevel(L) = (L - 1) * L * 50
//   L=1 → 0 XP,  L=2 → 100,  L=3 → 300,  L=5 → 1000,  L=10 → 4500,  L=15 → 10500
//
// Hidden the entire card for admin users — gamification is for traders, not operators.

import { Sparkles, TrendingUp, Trophy } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAuth } from "@/context/AuthContext";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

/** Total cumulative XP required to reach `level`. L=1 → 0. */
function xpForLevel(level: number): number {
  return (level - 1) * level * 50;
}

/** Smallest L such that xpForLevel(L) <= xp. Always >= 1. */
function levelForXp(xp: number): number {
  // Solve (L-1) * L * 50 >= xp  →  L^2 - L - xp/25 >= 0
  // Closed form: L = ceil((1 + sqrt(1 + 8*xp/50)) / 2)
  const L = (1 + Math.sqrt(1 + (8 * xp) / 50)) / 2;
  return Math.max(1, Math.floor(L + 0.0001));
}

export const XpLevelCard = () => {
  const { user } = useAuth();
  const { t } = useTranslation("dashboard");

  // Admins see ops dashboards, not gamification — hide the card entirely.
  if (!user || user.role === "admin") return null;

  const currentLevel = user.level ?? levelForXp(user.xp ?? 0);
  const totalXp = user.xp ?? 0;

  const currentLevelXp = xpForLevel(currentLevel);
  const nextLevelXp = xpForLevel(currentLevel + 1);
  const xpInLevel = Math.max(0, totalXp - currentLevelXp);
  const xpToNext = Math.max(0, nextLevelXp - totalXp);
  const xpSpan = Math.max(1, nextLevelXp - currentLevelXp); // avoid /0 at top
  const progressPct = Math.min(100, Math.round((xpInLevel / xpSpan) * 100));

  return (
    <Card className="relative overflow-hidden border-amber-500/20 bg-gradient-to-br from-amber-500/5 via-card/40 to-card/40">
      {/* Decorative blurred orb — same visual language as WelcomeBanner */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-12 -right-12 w-48 h-48 rounded-full bg-amber-500/10 blur-3xl"
      />

      <CardContent className="relative p-5">
        <div className="flex items-center gap-4 flex-wrap">
          {/* Level badge — large, centered, the visual anchor */}
          <div className="flex flex-col items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-amber-500/20 to-amber-500/5 border border-amber-500/30 shrink-0">
            <span className="text-[10px] uppercase tracking-wider text-amber-500/80 font-semibold leading-none">
              {t("level", "Level")}
            </span>
            <span className="text-2xl font-bold text-amber-500 leading-none mt-0.5">
              {currentLevel}
            </span>
          </div>

          {/* XP bar + meta */}
          <div className="flex-1 min-w-[200px] space-y-1.5">
            <div className="flex items-center justify-between gap-2 text-sm">
              <span className="font-semibold flex items-center gap-1.5">
                <Sparkles className="w-3.5 h-3.5 text-amber-500" />
                {totalXp.toLocaleString()} {t("xp", "XP")}
              </span>
              <span className="text-muted-foreground text-xs">
                {xpToNext > 0
                  ? t("xpToNext", `${xpToNext.toLocaleString()} XP to Level ${currentLevel + 1}`, {
                      amount: xpToNext.toLocaleString(),
                      level: currentLevel + 1,
                    })
                  : t("maxLevel", "Max level reached")}
              </span>
            </div>

            <Progress
              value={progressPct}
              className="h-2 [&>div]:bg-gradient-to-r [&>div]:from-amber-500 [&>div]:to-amber-400"
            />

            <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
              <span className="flex items-center gap-1">
                <TrendingUp className="w-3 h-3" />
                {t("levelProgress", `Level ${currentLevel} · ${progressPct}%`, {
                  level: currentLevel,
                  pct: progressPct,
                })}
              </span>
              {user.plan && user.plan !== "free" && (
                <Badge variant="outline" className="text-[10px] py-0 h-4 border-amber-500/30 text-amber-500">
                  <Trophy className="w-2.5 h-2.5 mr-0.5" />
                  {user.plan}
                </Badge>
              )}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
};
