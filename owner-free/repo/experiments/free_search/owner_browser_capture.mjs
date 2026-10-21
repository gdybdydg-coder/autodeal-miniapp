// Passive capture through documented CUA tab APIs. No HTTP client, API key,
// hidden application state, scheduler or sender. Caller navigates the listing.
// The active path opens only its visible native price button, never filters.
function validTicket(handoff) {
  const ticket = handoff.ticket;
  const now = Date.now()/1000;
  if (!ticket || ticket.version !== 1 || !Number.isFinite(ticket.issued_at) ||
      !Number.isFinite(ticket.expires_at) || now < ticket.issued_at || now >= ticket.expires_at)
    throw Error('handoff_expired_or_future');
  return ticket;
}

export async function openHandoffRange(tab, handoff) {
  const ticket = validTicket(handoff);
  if (await tab.url() !== ticket.listing_url) throw Error('handoff_url_mismatch');
  const action = await openListingRange(tab, ticket.listing_id);
  return {...await captureHandoff(tab, handoff), action};
}

export async function openListingRange(tab, expectedId) {
  const started = Date.now()/1000;
  await listingIdentity(tab, expectedId);
  const lower = tab.playwright.getByText(/^від [\d\s]+\$$/).filter({visible:true});
  const upper = tab.playwright.getByText(/^до [\d\s]+\$$/).filter({visible:true});
  let clicked = false;
  const lo = await lower.count(), hi = await upper.count();
  if (lo === 0 && hi === 0) {
    const button = tab.playwright.getByText('Середня ціна', {exact:true}).filter({visible:true});
    if (await button.count() !== 1) throw Error('native_price_button_missing_or_ambiguous');
    await button.click();
    clicked = true;
    await tab.playwright.domSnapshot();
    await lower.waitFor({state:'visible', timeoutMs:5000});
    await upper.waitFor({state:'visible', timeoutMs:5000});
  } else if (lo !== 1 || hi !== 1) throw Error('range_ambiguous');
  // No calculator navigation, guessed range, manual filter or API fallback.
  const capture = await captureRange(tab, expectedId);
  return {kind:'listing_native_price_button', clicked, started_at:started,
    completed_at:capture.observed_at, duration_seconds:capture.observed_at-started};
}

export async function captureHandoff(tab, handoff) {
  const ticket = validTicket(handoff);
  if (await tab.url() !== ticket.listing_url) throw Error('handoff_url_mismatch');
  const capture = await captureRange(tab, ticket.listing_id);
  if (capture.observed_at >= ticket.expires_at) throw Error('handoff_expired_during_capture');
  return {ticket, ticket_sha256:handoff.ticket_sha256, capture};
}

async function listingIdentity(tab, expectedId) {
  const url = await tab.url();
  if (!/^[1-9][0-9]{0,11}$/.test(expectedId) ||
      !new RegExp(`^https://auto\\.ria\\.com/uk/auto_[A-Za-z0-9_-]+_${expectedId}\\.html$`).test(url))
    throw Error('range_url_mismatch');
  const identity = await tab.playwright.getByText(new RegExp(`^ID авто\\s+${expectedId}$`)).innerText();
  if (identity.trim().split(/\s+/).at(-1) !== expectedId) throw Error('range_identity_mismatch');
  return {url, identity};
}

export async function captureRange(tab, expectedId) {
  const {url, identity} = await listingIdentity(tab, expectedId);
  const lower = await tab.playwright.getByText(/^від [\d\s]+\$$/).filter({visible:true}).allTextContents({});
  const upper = await tab.playwright.getByText(/^до [\d\s]+\$$/).filter({visible:true}).allTextContents({});
  if (lower.length !== 1 || upper.length !== 1) throw Error('range_ambiguous');
  return {kind:'public_market_range', listing_id:expectedId, listing_url:url,
    visible_listing_id:identity.trim().split(/\s+/).at(-1),
    lower_text:lower, upper_text:upper, observed_at:Date.now()/1000};
}

export async function captureCurrent(tab, expectedId) {
  if (!/^[1-9][0-9]{0,11}$/.test(expectedId)) throw Error('invalid_listing_id');
  const url = await tab.url();
  if (url !== `https://auto.ria.com/uk/price/average/?currentTab=byOmniId&omniId=${expectedId}`)
    throw Error('calculator_url_mismatch');
  const inputId = await tab.playwright.getByRole('textbox').evaluate(el => el.value);
  const identity = await tab.playwright.getByText(
    new RegExp(`Знайдено .+ за ID оголошення ${expectedId}`)).innerText();
  const rawListingUrl = await tab.playwright.getByRole('link', {
    name: 'Переглянути пропозицію→', exact: true}).getAttribute('href');
  if (!new RegExp(`^https?://auto\\.ria\\.com/uk/auto_[A-Za-z0-9_-]+_${expectedId}\\.html$`).test(rawListingUrl)) throw Error('listing_link_mismatch');
  const listingUrl = rawListingUrl.replace(/^http:/, 'https:');
  const mean = await tab.playwright.getByText('Середня ціна', {exact:false})
    .evaluateAll(els => els.filter(el => el.textContent.length < 200)
      .map(el => el.parentElement.innerText));
  const period = await tab.playwright.getByRole('combobox')
    .evaluate(el => el.options[el.selectedIndex].text);
  if (inputId !== expectedId || mean.length !== 1) throw Error('ambiguous_calculator_result');
  return {listing_id:expectedId, listing_url:listingUrl, calculator_url:url,
    source_listing_href:rawListingUrl, input_id:inputId, identity_text:identity.trim(), mean_text:mean, period,
    observed_at:Date.now()/1000};
}
