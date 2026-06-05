// Stripe webhook. Listens for checkout.session.completed and (optionally)
// emails the buyer with their download link. To enable email delivery:
//  1. Sign up at https://resend.com (free tier: 100 emails/day)
//  2. Get an API key
//  3. Add RESEND_API_KEY env var in Netlify
// For now, this just logs the event so you can confirm the webhook is firing.
const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY);

exports.handler = async (event) => {
  const sig = event.headers["stripe-signature"];
  let stripeEvent;
  try {
    stripeEvent = stripe.webhooks.constructEvent(
      event.body,
      sig,
      process.env.STRIPE_WEBHOOK_SECRET
    );
  } catch (e) {
    return { statusCode: 400, body: "Bad signature: " + e.message };
  }

  if (stripeEvent.type === "checkout.session.completed") {
    const session = stripeEvent.data.object;
    const email = session.customer_details && session.customer_details.email;
    const sid = session.id;
    const amount = (session.amount_total || 0) / 100;
    console.log(`[stripe-webhook] PAID ${email} $${amount} session=${sid}`);

    // Optional: email the buyer. Uncomment after setting RESEND_API_KEY.
    //
    // if (process.env.RESEND_API_KEY && email) {
    //   await fetch("https://api.resend.com/emails", {
    //     method: "POST",
    //     headers: {
    //       "Authorization": "Bearer " + process.env.RESEND_API_KEY,
    //       "Content-Type": "application/json",
    //     },
    //     body: JSON.stringify({
    //       from: "Activity Recorder <noreply@yourdomain.com>",
    //       to: email,
    //       subject: "Your Activity Recorder download",
    //       html: `<p>Thanks for buying!</p>
    //              <p><a href="${process.env.URL}/thanks.html?session_id=${sid}">Download here</a></p>`,
    //     }),
    //   });
    // }
  }

  return { statusCode: 200, body: "ok" };
};
