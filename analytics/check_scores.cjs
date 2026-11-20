"use strict";

const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const [htmlPath, inputPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const script = html.match(/<script>\s*("use strict";[\s\S]*?)<\/script>/)?.[1];
const boundary = script?.indexOf('$("search").onsubmit');
if (!script || !boundary || boundary < 0) {
  throw new Error("Die laufende Seite enthält keine unterstützte Scoring-Funktion.");
}

const sandbox = vm.createContext({input});
const result = vm.runInContext(script.slice(0, boundary) + `
  data=normalizeStatements(input.stock);
  checkBenchmark.rows=input.benchmarkRows;
  const scores=Array.from({length:4},(_,index)=>{
    selected=index;
    const [value, ,groups]=liveChecks();
    const rows=groups.flatMap(([,criteria])=>criteria);
    return {value, total:rows.length, available:rows.filter(row=>typeof row[2]==="boolean").length};
  });
  const revenue=annual("income").find(([date])=>date.startsWith("2026-"))?.[1];
  ({
    symbol:data.symbol, currency:data.info.currency, date:data.prices.at(-1)?.[0], scores,
    raw:{
      closingPrice:data.prices.at(-1)?.[1],
      revenue:v(revenue,"TotalRevenue"),
      operatingIncome:v(revenue,"OperatingIncome"),
      netIncome:v(revenue,"NetIncome"),
      dilutedEps:v(revenue,"DilutedEPS")
    }
  });
`, sandbox, {timeout: 5000, filename: path.basename(htmlPath)});
process.stdout.write(JSON.stringify(result));
