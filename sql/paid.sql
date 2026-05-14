with purchase_histories as (
    select
        purchase_date_month_jst
        , content_id
        , ex_comics_title_id
        , ex_comics_package_no
        , jdcn
        , name
        , sales_price
        , sales_gross
        , sales_unit
        , app_pf
    from
        `jumpplus-4a5f4.dataset_datamart_tables.report_plus_monthly_purchase_histories`
    where
        1 = 1
        and pm_desc = 1
        and is_subscription is false
        and sales_price > 0
        and work_title not in ('週刊少年ジャンプ')
)

select
    ex_comics_title_id
    , name
    , jdcn
    , sales_price
    , coalesce(sales_unit_iOS, 0) as sales_unit_iOS
    , coalesce(sales_gross_iOS, 0) as sales_gross_iOS
    , round(coalesce(sales_gross_iOS, 0) * 0.7, 0) as sales_gross_gross_iOS
    , coalesce(sales_unit_And, 0) as sales_unit_And
    , coalesce(sales_gross_And, 0) as sales_gross_And
    , round(coalesce(sales_gross_And, 0) * 0.7, 0) as sales_gross_gross_And
from
    purchase_histories
pivot (
    sum(sales_gross) as sales_gross
    , sum(sales_unit) as sales_unit
    for app_pf in ('iOS', 'And')
)
order by
    ex_comics_title_id
    , ex_comics_package_no
;
