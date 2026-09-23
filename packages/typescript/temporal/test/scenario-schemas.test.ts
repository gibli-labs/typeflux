import { describe, expect, it } from "vitest";

import { lintProviderSafe } from "../src/index.js";
import {
  consolidateMarketingReview,
  reviewMarketingClaim,
} from "../examples/financial-claims-marketing-review.js";
import { consolidateClaimReview, reviewEvidenceItem } from "../examples/insurance-claim-review.js";
import { assessCase, sendEmail } from "../examples/lifecycle-review.js";
import { assessDisclosure, finalizeDisclosure } from "../examples/regulated-disclosure-review.js";

// Every scenario activity's output schema must be provider-safe: strict
// structured-output endpoints reject schemas that fail these lint rules.
const scenarioActivities = [
  reviewMarketingClaim,
  consolidateMarketingReview,
  reviewEvidenceItem,
  consolidateClaimReview,
  assessCase,
  sendEmail,
  assessDisclosure,
  finalizeDisclosure,
];

describe("scenario activity output schemas", () => {
  it.each(scenarioActivities.map((activity) => [activity.name, activity] as const))(
    "%s output schema is provider-safe",
    (_name, activity) => {
      expect(lintProviderSafe(activity.outputProviderSchema)).toEqual([]);
    },
  );
});
