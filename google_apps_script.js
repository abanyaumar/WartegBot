const RESTAURANTS = ["Pisangan Lama","Kebagusan","Pejaten","Kranggan","Cibinong","Siaga Raya","Ragunan","Buncit Raya","WKB Tuban","WKB Bogor","Yogya UMY","Yogya ISI"];
const COLS_MAIN    = ["Date","Omzet","Belanja_Warung","Belanja_Pasar","Total_Belanja","Keuntungan","GoFood_Order","GoFood_Net","Notes"];
const COLS_PENG    = ["Periode","Beras","PLN","PDAM","Wifi","Sampah","Kasbon","Gaji","Lain_lain","Total","Catatan"];
const COLS_GOFOOD  = ["Periode","Total_Bruto","Total_Netto","Jumlah_Transaksi","Catatan"];
const COLS_BELANJA = ["Timestamp","Findings"];

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    const restaurant = data.restaurant;
    const type = data.type || "laporan_harian";
    if (!RESTAURANTS.includes(restaurant)) return resp({status:"error",message:"Cabang tidak valid"});
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    if (type === "laporan_harian") return saveMainReport(ss, restaurant, data);
    if (type === "pengeluaran")    return savePengeluaran(ss, restaurant, data);
    if (type === "gofood")         return saveGofood(ss, restaurant, data);
    if (type === "belanja_detail") return saveBelanjaDetail(ss, restaurant, data);
    return resp({status:"error",message:"Tipe tidak dikenal: " + type});
  } catch(err) { return resp({status:"error",message:err.toString()}); }
}

function doGet(e) {
  try {
    const action = e.parameter.action;
    if (action === "summary") {
      const restaurant = e.parameter.restaurant;
      const days = parseInt(e.parameter.days) || 10;
      const startDate = e.parameter.startDate || null;
      const periodTag = e.parameter.periodTag || null;
      return resp(getSummary(restaurant, days, startDate, periodTag));
    }
    if (action === "getData") { return resp(getData(e.parameter.restaurant)); }
    if (action === "getAllData") { return resp(getAllData()); }
    if (action === "debug") {
      var ss = SpreadsheetApp.getActiveSpreadsheet();
      var sh = ss.getSheetByName(e.parameter.restaurant || "Pisangan Lama");
      if (!sh) return resp({error:"sheet not found"});
      var vals = sh.getRange(2,1,5,1).getValues();
      var info = vals.map(function(r){
        return {raw: String(r[0]), type: typeof r[0], isDate: r[0] instanceof Date, val: r[0]};
      });
      return resp({rows: info});
    }
    return resp({status:"active",message:"Warteg Bot running!"});
  } catch(err) { return resp({status:"error",message:err.toString()}); }
}

function getData(restaurant) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(restaurant);
  if (!sheet || sheet.getLastRow() <= 1) return {status:"success",restaurant:restaurant,rows:[]};
  const raw = sheet.getRange(2,1,sheet.getLastRow()-1,9).getValues();
  const rows = raw.map(function(r) {
    var d = r[0];
    var dateStr = "";
    try {
      var parsed = new Date(d);
      if (!isNaN(parsed.getTime())) {
        dateStr = Utilities.formatDate(parsed, "Asia/Jakarta", "yyyy-MM-dd");
      }
    } catch(e) {}
    return {
      date:           dateStr,
      omzet:          Number(r[1])||0,
      belanja_warung: Number(r[2])||0,
      belanja_pasar:  Number(r[3])||0,
      total_belanja:  Number(r[4])||0,
      keuntungan:     Number(r[5])||0,
      gofood_order:   Number(r[6])||0,
      gofood_net:     Number(r[7])||0,
      notes:          String(r[8]||"")
    };
  }).filter(function(r){ return r.date && r.date.match(/^\d{4}-\d{2}-\d{2}$/); });
  return {status:"success",restaurant:restaurant,rows:rows};
}

function getAllData() {
  const result = {};
  RESTAURANTS.forEach(function(rn){ result[rn] = getData(rn).rows; });
  return {status:"success",data:result};
}

function saveMainReport(ss, restaurant, data) {
  let sheet = getOrCreateSheet(ss, restaurant, COLS_MAIN);
  const bw=Number(data.belanja_warung)||0, bp=Number(data.belanja_pasar)||0;
  // Bot sends correct omzet already (includes carry-over for WKB Tuban)
  const omzet = Number(data.omzet)||0;
  // WKB Tuban: only belanja_pasar is real expense; belanja_warung = carry-over tracking only
  const tb = (restaurant === "WKB Tuban") ? bp : (bw + bp);
  const k  = omzet - tb;
  const tgl=data.tanggal||new Date().toISOString().split("T")[0];
  // Dates stored as display date (no offset); toDateStr reads them back in Jakarta timezone
  const storedTgl = tgl;
  const lastRow=sheet.getLastRow();
  if (lastRow>1) {
    const dates=sheet.getRange(2,1,lastRow-1,1).getValues();
    const dup=dates.some(function(r){
      if(!r[0]) return false;
      var ex=(r[0] instanceof Date)?Utilities.formatDate(r[0],"Asia/Jakarta","yyyy-MM-dd"):String(r[0]).substring(0,10);
      return ex===storedTgl;
    });
    if(dup) return resp({status:"duplicate",message:"Data "+tgl+" sudah ada"});
  }
  const tglDate = new Date(storedTgl + "T12:00:00+07:00");
  sheet.appendRow([tglDate,omzet,bw,bp,tb,k,Number(data.gofood_order)||0,Number(data.gofood_net)||0,data.catatan||""]);
  const nr=sheet.getLastRow();
  sheet.getRange(nr,1).setNumberFormat("DD-MMM-YYYY");
  sheet.getRange(nr,2,1,7).setNumberFormat("#,##0");
  if(nr%2===0) sheet.getRange(nr,1,1,COLS_MAIN.length).setBackground("#EBF3FB");
  return resp({status:"success",type:"laporan_harian",restaurant:restaurant,tanggal:tgl,omzet:omzet,keuntungan:k});
}

function savePengeluaran(ss, restaurant, data) {
  const sheetName = restaurant + "_Pengeluaran";
  let sheet = getOrCreateSheet(ss, sheetName, COLS_PENG);
  sheet.appendRow([
    data.periode||"", Number(data.beras)||0, Number(data.pln)||0,
    Number(data.pdam)||0, Number(data.wifi)||0, Number(data.sampah)||0,
    Number(data.kasbon)||0, Number(data.gaji)||0, Number(data.lain_lain)||0,
    Number(data.total)||0, data.catatan||""
  ]);
  const nr=sheet.getLastRow();
  sheet.getRange(nr,2,1,9).setNumberFormat("#,##0");
  if(nr%2===0) sheet.getRange(nr,1,1,COLS_PENG.length).setBackground("#FFF3E0");
  return resp({status:"success",type:"pengeluaran",restaurant:restaurant});
}

function saveGofood(ss, restaurant, data) {
  const sheetName = restaurant + "_GoFood";
  let sheet = getOrCreateSheet(ss, sheetName, COLS_GOFOOD);
  sheet.appendRow([
    data.periode||"", Number(data.total_bruto)||0,
    Number(data.total_netto)||0, Number(data.jumlah_transaksi)||0, data.catatan||""
  ]);
  const nr=sheet.getLastRow();
  sheet.getRange(nr,2,1,3).setNumberFormat("#,##0");
  if(nr%2===0) sheet.getRange(nr,1,1,COLS_GOFOOD.length).setBackground("#E8F5E9");

  // Also update GoFood_Net (column H) in main sheet for the matching date
  if (data.tanggal) {
    const mainSheet = ss.getSheetByName(restaurant);
    if (mainSheet && mainSheet.getLastRow() > 1) {
      // Dates stored as display dates — compare directly
      const tgl = String(data.tanggal).substring(0, 10);
      const dates = mainSheet.getRange(2, 1, mainSheet.getLastRow()-1, 1).getValues();
      for (let i = 0; i < dates.length; i++) {
        const rowDate = dates[i][0];
        const rowDateStr = (rowDate instanceof Date)
          ? Utilities.formatDate(rowDate, "Asia/Jakarta", "yyyy-MM-dd")
          : String(rowDate).substring(0, 10);
        if (rowDateStr === tgl) {
          mainSheet.getRange(i + 2, 8).setValue(Number(data.total_netto) || 0);
          break;
        }
      }
    }
  }

  return resp({status:"success",type:"gofood",restaurant:restaurant});
}

function saveBelanjaDetail(ss, restaurant, data) {
  const sheetName = restaurant + "_BelanjaAudit";
  let sheet = getOrCreateSheet(ss, sheetName, COLS_BELANJA);
  sheet.appendRow([new Date(), data.findings||""]);
  const nr=sheet.getLastRow();
  sheet.getRange(nr,1).setNumberFormat("DD-MMM-YYYY HH:mm");
  if(nr%2===0) sheet.getRange(nr,1,1,COLS_BELANJA.length).setBackground("#FCE4EC");
  return resp({status:"success",type:"belanja_detail",restaurant:restaurant});
}

// Returns the next calendar month as "YYYY-MM" (e.g. "2026-05" → "2026-06")
function nextMonth(yyyymm) {
  var parts = yyyymm.split("-");
  var y = parseInt(parts[0]), m = parseInt(parts[1]);
  if (m === 12) return (y + 1) + "-01";
  return y + "-" + String(m + 1).padStart(2, "0");
}

// All dates in Google Sheet are stored 1 day early (convention from historical data).
// fmtDate adds +1 day so the displayed date matches the actual operational date.
function fmtDate(d) {
  if (!d) return "";
  var dt = (d instanceof Date) ? d : new Date(d);
  // Add 1 day to compensate for 1-day-early storage convention
  dt = new Date(dt.getTime() + 86400000);
  var ds = Utilities.formatDate(dt, "Asia/Jakarta", "yyyy-MM-dd");
  var p = ds.split("-");
  var MNAMES = ["Jan","Feb","Mar","Apr","Mei","Jun","Jul","Ags","Sep","Okt","Nov","Des"];
  return parseInt(p[2]) + " " + MNAMES[parseInt(p[1])-1] + " " + p[0];
}

// Returns the previous day of a YYYY-MM-DD string (for 1-day-early storage convention)
function prevDay(ymd) {
  return Utilities.formatDate(new Date(new Date(ymd + "T12:00:00Z").getTime() - 86400000), "UTC", "yyyy-MM-dd");
}

function getSummary(restaurant, days, startDateParam, periodTag) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let totalOmzet=0, totalBelanja=0, totalGofoodNetto=0, totalPengeluaran=0;
  let startDate="", endDate="", endDateStr="9999-12-31";
  const TZ = "Asia/Jakarta";

  // Convert date to YYYY-MM-DD string in Jakarta timezone for safe comparison
  function toDateStr(d) {
    if (!d) return "";
    return Utilities.formatDate(d instanceof Date ? d : new Date(d), TZ, "yyyy-MM-dd");
  }

  function getTargetRows(data) {
    // Sort ascending by date
    data.sort(function(a,b){ return new Date(a[0]) - new Date(b[0]); });
    if (startDateParam) {
      // Dates are stored as display dates in Jakarta timezone (no offset).
      // Filter directly by the provided start date range.
      var startMs  = new Date(startDateParam + "T00:00:00+07:00").getTime();
      var endMs    = startMs + (days - 1) * 86400000;
      var adjStart = Utilities.formatDate(new Date(startMs), TZ, "yyyy-MM-dd");
      var adjEnd   = Utilities.formatDate(new Date(endMs),   TZ, "yyyy-MM-dd");
      return data.filter(function(row){
        var ds = row[0] ? toDateStr(row[0]) : "";
        return ds >= adjStart && ds <= adjEnd;
      });
    } else {
      return data.slice(Math.max(0, data.length - days));
    }
  }

  const mainSheet = ss.getSheetByName(restaurant);
  if (mainSheet && mainSheet.getLastRow() > 1) {
    const data = mainSheet.getRange(2,1,mainSheet.getLastRow()-1,8).getValues();
    const target = getTargetRows(data);
    target.forEach(function(row){
      totalOmzet   += Number(row[1])||0;
      totalBelanja += (Number(row[2])||0) + (Number(row[3])||0);
    });
    if (target.length > 0) {
      startDate = fmtDate(target[0][0]);
      endDate   = fmtDate(target[target.length-1][0]);
      endDateStr = toDateStr(target[target.length-1][0]);
    }
  }

  const gofoodSheet = ss.getSheetByName(restaurant + "_GoFood");
  if (gofoodSheet && gofoodSheet.getLastRow() > 1) {
    const gdata = gofoodSheet.getRange(2,1,gofoodSheet.getLastRow()-1,3).getValues();
    if (startDateParam) {
      // For a specific period recap, use the most recently uploaded GoFood entry
      // (last row = latest upload = authoritative monthly total).
      const lastRow = gdata[gdata.length - 1];
      totalGofoodNetto = Number(lastRow[2])||0;
    } else {
      // No date filter: sum all (legacy/dashboard use)
      gdata.forEach(function(row){ totalGofoodNetto += Number(row[2])||0; });
    }
  }

  // Pengeluaran: filter by periodTag (P1/P2/P3) and month from startDateParam
  const pengSheet = ss.getSheetByName(restaurant + "_Pengeluaran");
  let totalPengeluaranP1 = 0, totalPengeluaranP2 = 0, totalPengeluaranP3 = 0;
  let totalKasbon = 0, totalKasbonP1 = 0, totalKasbonP2 = 0;
  if (pengSheet && pengSheet.getLastRow() > 1) {
    const rangeMonth = startDateParam ? startDateParam.substring(0, 7) : null;
    const pdata = pengSheet.getRange(2,1,pengSheet.getLastRow()-1,10).getValues();
    pdata.forEach(function(row){
      const prd = String(row[0]||"").trim();
      let match = false; let matchTag = null;
      if (!periodTag) {
        // No filter — include all rows; still parse P1/P2/P3 tag from data for breakdown
        match = true;
        if (prd.match(/^\d{4}-\d{2}-P[123]$/)) matchTag = prd.substring(8);
      } else if (prd.match(/^\d{4}-\d{2}-P[123]$/)) {
        const prdMonth = prd.substring(0, 7);
        const prdTag   = prd.substring(8);
        if (periodTag === "P3") {
          // Monthly recap spans up to two calendar months (e.g. 25 May–24 Jun).
          // Include pengeluaran tagged with startMonth OR the following month.
          const nm = nextMonth(rangeMonth);
          match = prdMonth === rangeMonth || prdMonth === nm;
          if (match) matchTag = prdTag;
        } else {
          match = prdMonth === rangeMonth && prdTag === periodTag;
          if (match) matchTag = prdTag;
        }
      } else {
        match = false;
      }
      if (match) {
        const amt = Number(row[9])||0;
        const kasbon = Number(row[6])||0;  // COLS_PENG index 6 = Kasbon
        totalPengeluaran += amt;
        totalKasbon += kasbon;
        if (matchTag === "P1") { totalPengeluaranP1 += amt; totalKasbonP1 += kasbon; }
        else if (matchTag === "P2") { totalPengeluaranP2 += amt; totalKasbonP2 += kasbon; }
        else if (matchTag === "P3") totalPengeluaranP3 += amt;
      }
    });
  }

  const periode = (startDate && endDate) ? (startDate + " - " + endDate) : "-";

  let dailyRows = [];
  const mainSheet2 = ss.getSheetByName(restaurant);
  if (mainSheet2 && mainSheet2.getLastRow() > 1) {
    const data2 = mainSheet2.getRange(2,1,mainSheet2.getLastRow()-1,8).getValues();
    const target2 = getTargetRows(data2);
    target2.forEach(function(row){
      dailyRows.push({
        date:           fmtDate(row[0]),
        _sortKey:       toDateStr(row[0]),
        omzet:          Number(row[1])||0,
        belanja_warung: Number(row[2])||0,
        belanja_pasar:  Number(row[3])||0,
        total_belanja:  (Number(row[2])||0)+(Number(row[3])||0),
        keuntungan:     Number(row[5])||0,
        gofood_net:     Math.round(Number(row[7])||0)
      });
    });
    // Sort by YYYY-MM-DD string = correct chronological order
    dailyRows.sort(function(a,b){ return a._sortKey > b._sortKey ? 1 : -1; });
    dailyRows.forEach(function(r){ delete r._sortKey; });
  }

  return {
    status: "success",
    restaurant: restaurant,
    periode: periode,
    total_omzet: totalOmzet,
    total_gofood_netto: totalGofoodNetto,
    total_belanja: totalBelanja,
    total_pengeluaran: totalPengeluaran,
    pengeluaran_p1: totalPengeluaranP1,
    pengeluaran_p2: totalPengeluaranP2,
    pengeluaran_p3: totalPengeluaranP3,
    kasbon_total: totalKasbon,
    kasbon_p1: totalKasbonP1,
    kasbon_p2: totalKasbonP2,
    days_counted: days,
    rows: dailyRows
  };
}

function getOrCreateSheet(ss, name, cols) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.appendRow(cols);
    const h = sheet.getRange(1,1,1,cols.length);
    h.setBackground("#1E3A5F");
    h.setFontColor("#FFFFFF");
    h.setFontWeight("bold");
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function resp(obj) { return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON); }
