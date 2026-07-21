import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { fetchFunds } from "../../api.js";
import FundCards from "./FundCards.jsx";
import FundDetail from "./FundDetail.jsx";

// Контейнер модуля «Фонды»: /funds — карточки, /funds/:code — деталка.
export default function FundsModule() {
  const { code } = useParams();
  const navigate = useNavigate();
  // валюта отображения ₽/$ /¥ — общая для карточек и деталки
  const [dispCcy, setDispCcy] = useState(() => localStorage.getItem("fund_ccy") || "RUB");
  useEffect(() => { localStorage.setItem("fund_ccy", dispCcy); }, [dispCcy]);

  const q = useQuery({ queryKey: ["funds"], queryFn: fetchFunds });

  if (code) {
    return (
      <FundDetail
        code={code}
        dispCcy={dispCcy}
        onSetCcy={setDispCcy}
        onBack={() => navigate("/funds")}
      />
    );
  }
  return (
    <FundCards
      funds={q.data || []}
      status={q.isPending ? "loading" : q.isError ? "error" : "ready"}
      errMsg={q.error?.message || ""}
      dispCcy={dispCcy}
      onSetCcy={setDispCcy}
      onSelect={(c) => navigate(`/funds/${encodeURIComponent(c)}`)}
      onReload={q.refetch}
    />
  );
}
