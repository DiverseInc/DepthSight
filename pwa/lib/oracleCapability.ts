// pwa/lib/oracleCapability.ts
//
// THE ORACLE IS NOT AVAILABLE TO EVERYONE.
//
// It used to be a constant, in the client:
//
//     const canUseOracle = true; // Oracle is available to everyone
//     const [oracleRegime, setOracleRegime] = useState("1");
//     const [oracleConfidence, setOracleConfidence] = useState(95);
//
// on a deployment whose api container logged, once per worker PID:
//
//     WARNING [api.simulation_router:122] - Oracle model not found at
//     /app/data/oracle_model.joblib
//
// There is no `train_oracle.py` in this repository, so nothing produces
// that file: the model is not merely missing, it does not exist yet. The
// 95 the customer saw was a CONSTANT -- never a model output.
//
// So the client asks the SERVER, and this module is the one place that
// interprets the answer. It is deliberately dependency-free and pure: the
// decision "may I show an oracle regime / confidence?" must have exactly
// one implementation, and it must be testable without a browser.

/** The server's answer about the oracle. */
export interface OracleCapability {
	/**
	 * True only when a real oracle model is loaded and usable on the
	 * server. False on every deployment today.
	 */
	oracleAvailable: boolean;
}

/**
 * May the client show an oracle regime and an oracle confidence?
 *
 * True ONLY when the server says a real model is loaded. Everything else
 * -- a failed probe, a missing field, a null capability -- is NO.
 *
 * Failing CLOSED is the point: if the probe cannot be completed, the
 * oracle is not offered. The alternative (assume it is there until the
 * server says otherwise) is how a client ends up telling a customer that
 * a 95% confidence exists when no model has ever produced one.
 */
export function oracleIsUsable(
	capability: OracleCapability | null | undefined,
): boolean {
	return capability?.oracleAvailable === true;
}

/**
 * The oracle settings to send when the oracle is NOT usable.
 *
 * Absent, not zero and not a default: `null` means "there is no oracle
 * filter here", which is what the server's own contract expects for a
 * strategy that is not filtering by regime. A number here would be a
 * measurement nobody made.
 */
export const NO_ORACLE_SETTINGS = {
	oracleRegime: null,
	oracleConfidence: null,
} as const;
