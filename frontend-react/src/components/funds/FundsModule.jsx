import { useCallback, useEffect, useState } from "react";
import { fetchFunds, UnauthorizedError } from "../../api.js";
import FundCards from "./FundCards.jsx";
import FundDetail from "./FundDetail.jsx";

// Контейнер модуля «Фонды»: список карточек ↔ деталка выбранного фонда.
export default function FundsModule({ onLogout }) {
  const [funds, setFunds] = useState([]);
  const [status, setStatus] = useState("loading"); // loading | ready | error
  const [errMsg, setErrMsg] = useState("");
  const [selCode, setSelCode] = useState(() => localStorage.getItem("fund_sel") || null);
  // валюта отображения ₽/$ /¥ — общая для карточек и деталки
  const [dispCcy, setDispCcy] = useState(() => localStorage.getItem("fund_ccy") || "RUB");

  useEffect(() => {
    if (selCode) localStorage.setItem("fund_sel", selCode);
    else localStorage.removeItem("fund_sel");
  }, [selCode]);
  useEffect(() => { localStorage.setItem("fund_ccy", dispCcy); }, [dispCcy]);

  const load = useCallback(async () => {
    setStatus("loading");
    try {
      const list = await fetchFunds();
      setFunds(list);
      setStatus("ready");
    } catch (e) {
      if (e instanceof UnauthorizedError) { onLogout(); return; }
      setErrMsg(e.message);
      setStatus("error");
    }
  }, [onLogout]);

  useEffect(() => { load(); }, [load]);

  if (selCode) {
    return (
      <FundDetail
        code={selCode}
        dispCcy={dispCcy}
        onSetCcy={setDispCcy}
        onBack={() => { setSelCode(null); load(); }}
        onLogout={onLogout}
      />
    );
  }
  return (
    <FundCards
      funds={funds}
      status={status}
      errMsg={errMsg}
      dispCcy={dispCcy}
      onSetCcy={setDispCcy}
      onSelect={setSelCode}
      onReload={load}
      onLogout={onLogout}
    />
  );
}
