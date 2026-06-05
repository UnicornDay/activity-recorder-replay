// Serves a zip of the source code, but only after verifying the
// session_id corresponds to a paid Stripe checkout session.
const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY);
const archiver = require("archiver");
const fs = require("fs");
const path = require("path");

// Files/folders to include in the buyer zip
const ROOT = path.resolve(__dirname, "../..");
const INCLUDE_PATTERNS = [
  "activity_gui.py",
  "requirements.txt",
  "README.md",
];
const EXCLUDE_NAMES = new Set([
  "__pycache__", ".git", "node_modules", "site",
  "netlify", ".netlify", "build", "dist", ".claude",
  "activity_logs", ".gitignore", "package.json", "package-lock.json",
]);

function shouldInclude(name) {
  return INCLUDE_PATTERNS.includes(name);
}

exports.handler = async (event) => {
  const sid = event.queryStringParameters && event.queryStringParameters.session_id;
  if (!sid) {
    return { statusCode: 400, body: "Missing session_id" };
  }

  try {
    const session = await stripe.checkout.sessions.retrieve(sid);
    if (session.payment_status !== "paid") {
      return { statusCode: 403, body: "Payment not completed" };
    }
  } catch (e) {
    return { statusCode: 403, body: "Invalid session" };
  }

  // Build the zip in memory
  return new Promise((resolve) => {
    const chunks = [];
    const archive = archiver("zip", { zlib: { level: 9 } });
    archive.on("data", (c) => chunks.push(c));
    archive.on("end", () => {
      const buf = Buffer.concat(chunks);
      resolve({
        statusCode: 200,
        headers: {
          "content-type": "application/zip",
          "content-disposition": 'attachment; filename="activity_recorder_source.zip"',
        },
        body: buf.toString("base64"),
        isBase64Encoded: true,
      });
    });
    archive.on("error", (e) => {
      resolve({ statusCode: 500, body: "Zip error: " + e.message });
    });

    for (const name of INCLUDE_PATTERNS) {
      const p = path.join(ROOT, name);
      if (fs.existsSync(p) && fs.statSync(p).isFile()) {
        archive.file(p, { name });
      }
    }
    archive.finalize();
  });
};
