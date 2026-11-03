// src/components/strategies/StrategyEditorSettingsCards.tsx
//
// The two settings the client types in the strategy editor and the server has
// to honour:
//
//   1. Risk limits      -> `config_data.risk_settings`
//   2. Strategy class   -> `config_data.strategy_name`
//
// Both are rendered here so the editor panel stays readable and so the
// validation rules live in one place (`./strategyEditorSettings.ts`).

import { AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
	BOT_REGISTERED_STRATEGY_CLASSES,
	SERVER_RISK_FLOOR_PCT,
	divergesFromVisualBuilder,
	hasServerRiskFloors,
} from "./strategyEditorSettings";
import type {
	StrategyClassErrorCode,
	StrategyRiskDraft,
	StrategyRiskErrors,
	StrategyRiskField,
} from "./strategyEditorSettings";

const SELECT_CLASS =
	"flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50";

interface RiskFieldMeta {
	field: StrategyRiskField;
	labelKey: string;
	labelDefault: string;
	descKey: string;
	descDefault: string;
	step: string;
}

const RISK_FIELD_META: RiskFieldMeta[] = [
	{
		field: "risk_per_trade_pct",
		labelKey: "configPanel.riskPerTradeLabel",
		labelDefault: "Risk Per Trade (%)",
		descKey: "configPanel.riskPerTradeDesc",
		descDefault: "Percentage of account equity risked on a single trade.",
		step: "0.1",
	},
	{
		field: "daily_loss_limit_pct",
		labelKey: "configPanel.dailyLossLimitLabel",
		labelDefault: "Daily Loss Limit (%)",
		descKey: "configPanel.dailyLossLimitDesc",
		descDefault:
			"Stop trading for the day once daily losses reach this percentage of equity.",
		step: "0.5",
	},
	{
		field: "max_drawdown_pct",
		labelKey: "configPanel.maxDrawdownLabel",
		labelDefault: "Max Drawdown (%)",
		descKey: "configPanel.maxDrawdownDesc",
		descDefault:
			"Largest peak-to-trough equity decline tolerated before the strategy is stopped.",
		step: "1",
	},
	{
		field: "position_size_pct",
		labelKey: "configPanel.positionSizeLabel",
		labelDefault: "Position Size (%)",
		descKey: "configPanel.positionSizeDesc",
		descDefault: "Percentage of account equity committed to each position.",
		step: "1",
	},
];

interface StrategyRiskSettingsSectionProps {
	/** The text the client has typed for each field. */
	value: StrategyRiskDraft;
	errors: StrategyRiskErrors;
	onChange: (field: StrategyRiskField, raw: string) => void;
	disabled?: boolean;
}

export function StrategyRiskSettingsSection({
	value,
	errors,
	onChange,
	disabled = false,
}: StrategyRiskSettingsSectionProps) {
	const { t } = useTranslation(["strategy-editor", "common"]);

	const errorText = (
		code: StrategyRiskErrors[StrategyRiskField],
		field: StrategyRiskField,
	) => {
		switch (code) {
			case "notANumber":
				return t("configPanel.riskErrorNotANumber", "Enter a number.");
			case "notPositive":
				return t("configPanel.riskErrorNotPositive", "Must be greater than zero.");
			case "aboveFloor":
				return t("configPanel.riskErrorAboveFloor", {
					defaultValue: `The server rejects a value above ${SERVER_RISK_FLOOR_PCT[field]}%.`,
					floor: SERVER_RISK_FLOOR_PCT[field],
				});
			case "positionExceedsRisk":
				return t(
					"configPanel.riskErrorPositionExceedsRisk",
					"Position size cannot exceed the risk per trade.",
				);
			default:
				return null;
		}
	};

	return (
		<Card>
			<CardHeader>
				<CardTitle>{t("configPanel.riskSettingsTitle", "Risk Settings")}</CardTitle>
				<CardDescription>
					{t(
						"configPanel.riskSettingsDescription",
						"Risk limits are saved with the strategy and sent to the server on deploy.",
					)}
				</CardDescription>
			</CardHeader>
			<CardContent className="space-y-4">
				<div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
					<AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
					{hasServerRiskFloors() ? (
						<span>
							{t("configPanel.riskServerFloorsNotice", {
								defaultValue: `Server floors: ${SERVER_RISK_FLOOR_PCT.daily_loss_limit_pct}% daily loss, ${SERVER_RISK_FLOOR_PCT.risk_per_trade_pct}% risk per trade, ${SERVER_RISK_FLOOR_PCT.max_drawdown_pct}% drawdown, ${SERVER_RISK_FLOOR_PCT.position_size_pct}% position size. A limit can only be tightened, never loosened.`,
								dailyLossFloor: SERVER_RISK_FLOOR_PCT.daily_loss_limit_pct,
								riskPerTradeFloor: SERVER_RISK_FLOOR_PCT.risk_per_trade_pct,
								drawdownFloor: SERVER_RISK_FLOOR_PCT.max_drawdown_pct,
								positionSizeFloor: SERVER_RISK_FLOOR_PCT.position_size_pct,
							})}
						</span>
					) : (
						<span>
							{t(
								"configPanel.riskServerFloorsLoading",
								"The server's risk limits have not loaded yet, so no limit can be checked or saved until they do.",
							)}
						</span>
					)}
				</div>

				<div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
					{RISK_FIELD_META.map((meta) => {
						const fieldError = errors?.[meta.field];
						const inputId = `risk-${meta.field}`;
						return (
							<div key={meta.field} className="space-y-1">
								<Label htmlFor={inputId}>
									{t(meta.labelKey, meta.labelDefault)}
								</Label>
								<Input
									id={inputId}
									type="number"
									inputMode="decimal"
									step={meta.step}
									min="0"
									value={value[meta.field]}
									disabled={disabled}
									aria-invalid={Boolean(fieldError)}
									aria-describedby={`${inputId}-desc`}
									onChange={(event) =>
										onChange(meta.field, event.target.value)
									}
								/>
								<p id={`${inputId}-desc`} className="text-xs text-muted-foreground">
									{t(meta.descKey, meta.descDefault)}{" "}
									{t("configPanel.riskServerFloor", {
										defaultValue:
											typeof SERVER_RISK_FLOOR_PCT[meta.field] === "number"
												? `Server floor: ${SERVER_RISK_FLOOR_PCT[meta.field]}%.`
												: "The server's limit for this field has not loaded yet.",
										floor: SERVER_RISK_FLOOR_PCT[meta.field],
									})}
								</p>
								{fieldError ? (
									<p role="alert" className="text-xs text-destructive">
										{errorText(fieldError, meta.field)}
									</p>
								) : null}
							</div>
						);
					})}
				</div>
			</CardContent>
		</Card>
	);
}

interface StrategyEngineSectionProps {
	value: string;
	onChange: (next: string) => void;
	error?: StrategyClassErrorCode;
	disabled?: boolean;
}

export function StrategyEngineSection({
	value,
	onChange,
	error,
	disabled = false,
}: StrategyEngineSectionProps) {
	const { t } = useTranslation(["strategy-editor", "common"]);
	const known = BOT_REGISTERED_STRATEGY_CLASSES.includes(
		value as (typeof BOT_REGISTERED_STRATEGY_CLASSES)[number],
	);
	// A class that came back from the server but is not in the client-side
	// mirror is still shown, so the saved value is never silently replaced.
	const classOptions: string[] = known
		? [...BOT_REGISTERED_STRATEGY_CLASSES]
		: [value, ...BOT_REGISTERED_STRATEGY_CLASSES];

	return (
		<Card>
			<CardHeader>
				<CardTitle>
					{t("configPanel.strategyEngineLabel", "Strategy Engine")}
				</CardTitle>
				<CardDescription>
					{t(
						"configPanel.strategyEngineDescription",
						"The trading class this strategy runs as. It is saved with the strategy and sent to the server on deploy.",
					)}
				</CardDescription>
			</CardHeader>
			<CardContent className="space-y-2">
				<Label htmlFor="strategy-engine-class">
					{t("configPanel.strategyEngineClassLabel", "Strategy Class")}
				</Label>
				<select
					id="strategy-engine-class"
					className={SELECT_CLASS}
					value={value}
					disabled={disabled}
					aria-invalid={Boolean(error)}
					onChange={(event) => onChange(event.target.value)}
				>
					{classOptions.map((className) => (
						<option key={className} value={className}>
							{className}
						</option>
					))}
				</select>
				<p className="text-xs text-muted-foreground">
					{t(
						"configPanel.strategyEngineRegisteredNotice",
						"Only classes the trading bot registers can run. A class the bot does not know will not start.",
					)}
				</p>
				{error === "notRegistered" ? (
					<p role="alert" className="text-xs text-destructive">
						{t(
							"configPanel.strategyEngineNotRegistered",
							"This strategy class is not in the trading bot's known list, so it may not start.",
						)}
					</p>
				) : null}
				{error === "empty" ? (
					<p role="alert" className="text-xs text-destructive">
						{t("configPanel.strategyEngineRequired", "Pick a strategy class.")}
					</p>
				) : null}
				{!error && divergesFromVisualBuilder(value) ? (
					<div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
						<AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
						<span>
							{t("configPanel.strategyEngineDivergenceWarning", {
								defaultValue:
									"Your visual blocks are executed by VisualBuilderStrategy. With this class the bot runs its own logic instead, and backtests still run VisualBuilderStrategy.",
								className: value,
							})}
						</span>
					</div>
				) : null}
			</CardContent>
		</Card>
	);
}
