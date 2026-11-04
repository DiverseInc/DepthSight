// src/features/hft-dashboard/lib/oracleConfig.ts

import type { HftConfig } from "../types/hft.types";

/**
 * THE ORACLE FILTER MAY ONLY BE ON WHEN A REAL MODEL IS LOADED.
 *
 * The HFT dashboard's Oracle toggle used to read `use_oracle: true` out of
 * thin air -- in the client's own local config, and in the server's stored
 * HFT config default -- on a deployment whose api container logs, once per
 * worker:
 *
 *     WARNING [api.simulation_router:122] - Oracle model not found at
 *     /app/data/oracle_model.joblib
 *
 * A toggle that says ON tells the customer a regime filter is running.
 * With no model, nothing can produce a regime to filter on, so the filter
 * is not running and the control must not say it is.
 *
 * DERIVED, NOT STORED: this is applied on the way out, so that re-reading
 * the stored config -- which still says `use_oracle: true` -- cannot switch
 * the fiction back on.
 *
 * When the oracle IS available the config is returned untouched, so the
 * customer's own setting survives.
 */
export function applyOracleAvailability(
	config: HftConfig,
	oracleAvailable: boolean,
): HftConfig {
	if (oracleAvailable) return config;
	return { ...config, use_oracle: false };
}
