import { expect, it } from "vitest";
import { collectMetadata, type MetadataContributor } from "../src/metadata.js";

it("composes typed contributions and refuses namespace collisions", () => {
  const contributor: MetadataContributor<{engine: string}> = {contribute: context => ({"typeflux.runtime": context})};
  expect(collectMetadata({engine: "local"}, [contributor])).toEqual({"typeflux.runtime": {engine: "local"}});
  expect(() => collectMetadata({engine: "local"}, [contributor, contributor])).toThrow(/duplicate metadata contribution/);
});
