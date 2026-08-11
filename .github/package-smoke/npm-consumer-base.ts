import { Bursar, PricingEngine } from "@zonastery/bursar";
import {
  BursarRuntime,
  S3BillingArchive,
  type S3ArchiveClient,
  type S3PutObjectOptions,
} from "@zonastery/bursar/node";
import { PROVIDER_ENVIRONMENTS, noopLogger } from "@zonastery/bursar/providers";

const client: S3ArchiveClient = {
  async send(_command: unknown) {
    return { VersionId: "version-1" };
  },
};
const putObject: S3PutObjectOptions = {
  ServerSideEncryption: "aws:kms",
  ChecksumAlgorithm: "SHA256",
};

new S3BillingArchive({ bucket: "billing-archive", client, putObject });
void Bursar;
void PricingEngine;
void BursarRuntime;
void PROVIDER_ENVIRONMENTS;
void noopLogger;
